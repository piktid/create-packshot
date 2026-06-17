#!/usr/bin/env python3
"""
Minimal script to process a garment folder with create-packshot.

This script performs the basic workflow:
1. Create a project
2. Upload garment images
3. Build instructions (from CLI flags or JSON file)
4. Create create-packshot job
5. Monitor job progress
6. Download results

Authentication uses an API token generated at https://app.on-model.com/profile?tab=tokens

Unlike flat-to-model and model-swap, create-packshot does NOT require an identity:
outputs are product-only (ghost-mannequin, flat-lay, marketing-ready, or white-cutout).
"""

import argparse
import http.client
import json
import random
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


PACKSHOT_STYLES = ["flat_lay", "ghost_mannequin", "marketing_ready", "white_cutout", "free"]
PACKSHOT_FRAMINGS = ["square_packshot", "tall_3_4", "wide_4_3", "full_frame"]
PACKSHOT_ANGLES = ["front", "back", "three_quarter", "top_down", "detail_macro"]
PACKSHOT_SHADOWS = ["auto", "none", "contact", "soft_drop", "natural"]


class CreatePackshot:
    def __init__(self, base_url, token, input_folder, output_folder="output",
                 style="ghost_mannequin", prompt=None, background=None,
                 framing=None, angle=None, shadow=None, surface=None,
                 num_variations=1, size=None, aspect_ratio=None, fmt=None, seed=None,
                 instructions_file=None, image_notes=None,
                 model="auto", use_anchor=False, anchor_index=0, post_process=False):
        self.base_url = base_url.rstrip("/")
        self.input_folder = Path(input_folder)
        self.output_folder = Path(output_folder)

        # Instruction parameters (simple mode)
        self.style = style
        self.prompt = prompt
        self.background = background
        self.framing = framing
        self.angle = angle
        self.shadow = shadow
        self.surface = surface
        self.num_variations = num_variations
        self.size = size
        self.aspect_ratio = aspect_ratio
        self.fmt = fmt
        self.seed = seed

        # Advanced mode
        self.instructions_file = Path(instructions_file) if instructions_file else None

        # Per-image annotations
        self.image_notes = image_notes or []

        # Job-level generation options
        self.model = model
        self.use_anchor = use_anchor
        self.anchor_index = anchor_index
        self.post_process = post_process

        self.access_token = token
        self.project_id = None
        self.project_name = None

    def get_auth_headers(self):
        """Get headers with Bearer token."""
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request_with_retry(self, method, url, max_retries=5, initial_delay=1.0, max_delay=60.0, **kwargs):
        """Make an authenticated request with retry on rate limiting (429).

        Args:
            method: HTTP method ('get', 'post', etc.)
            url: Full URL to request
            max_retries: Maximum retry attempts for 429 responses (default: 5)
            initial_delay: Initial backoff delay in seconds (default: 1.0)
            max_delay: Maximum delay between retries (default: 60.0)
            **kwargs: Additional arguments passed to requests (json, params, timeout, etc.)

        Returns:
            Response object
        """
        delay = initial_delay
        request_func = getattr(requests, method.lower())

        for attempt in range(max_retries + 1):
            headers = {**kwargs.pop("headers", {}), **self.get_auth_headers()}
            response = request_func(url, headers=headers, **kwargs)

            # Handle 401 - token expired or invalid
            if response.status_code == 401:
                print("Token expired or invalid. Generate a new one at https://app.on-model.com/profile?tab=tokens")
                return response

            # Not rate limited - return immediately
            if response.status_code != 429:
                return response

            # Rate limited (429) - retry with exponential backoff + jitter
            if attempt < max_retries:
                jitter = delay * 0.2 * (2 * random.random() - 1)
                wait_time = min(delay + jitter, max_delay)
                print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry {attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                delay = min(delay * 2, max_delay)
            else:
                print(f"Rate limited (429). Max retries ({max_retries}) exceeded.")

        return response

    def create_project(self, project_name):
        """Create a project on the API server."""
        print(f"Creating project '{project_name}'...")

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/project",
                json={"project_name": project_name}
            )

            if response.status_code in [200, 201]:
                data = response.json()
                self.project_id = data["project_id"]
                self.project_name = data["project_name"]
                print(f"Project created: {self.project_id}")
                return True
            elif response.status_code == 409:
                # Project already exists, get its ID from the response
                print(f"Project '{project_name}' already exists")
                data = response.json()
                if "project_id" in data:
                    self.project_id = data["project_id"]
                    self.project_name = project_name
                    print(f"Using existing project: {self.project_id}")
                    return True
                # Fallback: list projects to find the matching one
                return self._find_project_by_name(project_name)
            else:
                print(f"Failed to create project: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"Error creating project: {e}")
            return False

    def _find_project_by_name(self, project_name):
        """Find an existing project by name via the list endpoint."""
        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/project",
                params={"per_page": 100}
            )
            if response.status_code == 200:
                data = response.json()
                for project in data.get("projects", []):
                    if project.get("project_text") == project_name:
                        self.project_id = project.get("project_key", project.get("project_id"))
                        self.project_name = project_name
                        print(f"Found existing project: {self.project_id}")
                        return True
            print(f"Could not find project '{project_name}'")
            return False
        except Exception as e:
            print(f"Error listing projects: {e}")
            return False

    def get_upload_url(self, filename):
        """Get a pre-signed upload URL for an image."""
        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/upload",
                json={"filename": filename}
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get upload URL: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error getting upload URL: {e}")
            return None

    def upload_image(self, upload_url, file_path, content_type):
        """Upload an image to S3 using the pre-signed URL."""
        try:
            with open(file_path, "rb") as f:
                image_data = f.read()

            parsed = urlparse(upload_url)

            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
            else:
                conn = http.client.HTTPConnection(parsed.netloc, timeout=120)

            path = parsed.path
            if parsed.query:
                path = f"{path}?{parsed.query}"

            headers = {"Content-Type": content_type}
            conn.request("PUT", path, body=image_data, headers=headers)
            response = conn.getresponse()
            response.read()  # Read response to complete request
            conn.close()

            return response.status in [200, 201]
        except Exception as e:
            print(f"Error uploading image: {e}")
            return False

    def upload_garment_images(self):
        """Upload all garment images from the input folder."""
        if not self.input_folder.exists():
            print(f"Input folder not found: {self.input_folder}")
            print(f"Checked path: {self.input_folder.absolute()}")
            return []

        # Find all image files (skip dotfiles and hidden)
        image_extensions = [".jpg", ".jpeg", ".png"]
        image_files = [
            f for f in sorted(self.input_folder.iterdir())
            if f.is_file()
            and f.suffix.lower() in image_extensions
            and not f.name.startswith(".")
        ]

        if not image_files:
            print(f"No images found in {self.input_folder}")
            return []

        if len(image_files) > 10:
            print(f"Warning: found {len(image_files)} images; create-packshot accepts max 10. Truncating to first 10.")
            image_files = image_files[:10]

        print(f"Found {len(image_files)} garment images to upload")

        # Create project if not already created
        if not self.project_name:
            project_name = self.input_folder.name
            if not self.create_project(project_name):
                return []

        file_ids = []

        for image_file in image_files:
            print(f"Uploading {image_file.name}...")

            upload_info = self.get_upload_url(image_file.name)
            if not upload_info:
                print(f"Failed to get upload URL for {image_file.name}")
                continue

            upload_url = upload_info["upload_url"]
            # Fix URL scheme if needed
            if self.base_url.startswith("https://") and upload_url.startswith("http://"):
                upload_url = upload_url.replace("http://", "https://", 1)

            success = self.upload_image(
                upload_url,
                image_file,
                upload_info["content_type"]
            )

            if success:
                file_ids.append(upload_info["file_id"])
                print(f"Uploaded: {image_file.name} -> {upload_info['file_id']}")
            else:
                print(f"Failed to upload: {image_file.name}")

        print(f"Successfully uploaded {len(file_ids)}/{len(image_files)} garment images")
        return file_ids

    def _build_instructions(self):
        """Build the instructions array from CLI flags or a JSON file.

        Advanced mode (--instructions-file): Load instructions from a JSON file.
        Simple mode (CLI flags): Build a single instruction from individual flags.

        Returns:
            list[dict]: Instructions array to send to the API.
        """
        # Advanced mode: load from JSON file
        if self.instructions_file:
            if not self.instructions_file.exists():
                print(f"Instructions file not found: {self.instructions_file}")
                return None
            try:
                with open(self.instructions_file, "r") as f:
                    data = json.load(f)
                # Accept either a list or a dict with an "instructions" key
                if isinstance(data, list):
                    instructions = data
                elif isinstance(data, dict) and "instructions" in data:
                    instructions = data["instructions"]
                else:
                    print("Invalid instructions file: expected a list or {\"instructions\": [...]}")
                    return None
                print(f"Loaded {len(instructions)} instructions from {self.instructions_file.name}")
                return instructions
            except json.JSONDecodeError as e:
                print(f"Error parsing instructions file: {e}")
                return None

        # Simple mode: build a single instruction from CLI flags
        instruction = {"style": self.style}

        if self.prompt:
            instruction["prompt"] = self.prompt
        if self.background:
            instruction["background"] = self.background
        if self.framing:
            instruction["framing"] = self.framing
        if self.angle:
            instruction["angle"] = self.angle
        if self.shadow:
            instruction["shadow"] = self.shadow
        if self.surface:
            instruction["surface"] = self.surface
        if self.seed is not None:
            instruction["seed"] = self.seed
        if self.num_variations and self.num_variations > 1:
            instruction["num_variations"] = self.num_variations

        options = {}
        if self.size:
            options["size"] = self.size
        if self.aspect_ratio:
            options["ar"] = self.aspect_ratio
        if self.fmt:
            options["format"] = self.fmt
        if options:
            instruction["options"] = options

        return [instruction]

    def create_job(self, file_ids, instructions):
        """Create a create-packshot job.

        The `images` field accepts two formats:
        - Simple (list of UUIDs): ["uuid-1", "uuid-2"]
        - Annotated (list of objects): [{"file_id": "uuid-1", "note": "front view"}, {"file_id": "uuid-2"}]

        Annotations are optional per-image notes that highlight context the AI might
        otherwise miss (which view the photo shows, fabric peculiarities, lining
        details to preserve, etc.).
        """
        print("Creating create-packshot job...")

        # Build images payload - use annotated format if notes are provided
        if self.image_notes:
            images_payload = []
            for i, fid in enumerate(file_ids):
                entry = {"file_id": fid}
                if i < len(self.image_notes) and self.image_notes[i]:
                    entry["note"] = self.image_notes[i]
                images_payload.append(entry)
        else:
            images_payload = file_ids

        # Job-level options. use_anchor defaults to False for create-packshot
        # (each instruction often uses a different style on purpose, so
        # cross-instruction cohesion is undesirable by default).
        job_options = {"model": self.model}
        if self.use_anchor:
            job_options["use_anchor"] = True
            if self.anchor_index:
                job_options["anchor_index"] = self.anchor_index

        payload = {
            "project_id": self.project_id,
            "images": images_payload,
            "instructions": instructions,
            "post_process": self.post_process,
            "options": job_options,
        }

        try:
            response = self._request_with_retry(
                "post",
                f"{self.base_url}/create-packshot",
                json=payload
            )

            if response.status_code == 202:
                data = response.json()
                job_id = data["job_id"]
                total_outputs = data.get("total_outputs", len(instructions))
                print(f"Job created: {job_id} ({total_outputs} outputs)")
                return job_id
            else:
                print(f"Failed to create job: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"Error creating job: {e}")
            return None

    def wait_for_job(self, job_id, max_wait_time=1200, check_interval=5):
        """Wait for job to complete."""
        print("Waiting for job to complete...")

        start_time = time.time()
        max_retries_404 = 10
        retry_count = 0

        time.sleep(5)  # Initial delay

        while True:
            if time.time() - start_time > max_wait_time:
                print(f"Timeout: Job took longer than {max_wait_time} seconds")
                return None

            try:
                response = self._request_with_retry(
                    "get",
                    f"{self.base_url}/jobs/{job_id}/status"
                )

                if response.status_code == 404:
                    retry_count += 1
                    if retry_count <= max_retries_404:
                        print(f"Job not found yet (attempt {retry_count}/{max_retries_404}), waiting...")
                        time.sleep(30)
                        continue
                    else:
                        print(f"Job {job_id} not found after {max_retries_404} retries")
                        return None

                if response.status_code != 200:
                    print(f"Failed to get status: {response.status_code}")
                    print(f"Response: {response.text}")
                    return None

                retry_count = 0
                status_data = response.json()
                status = status_data["status"]
                progress = status_data.get("progress", 0)

                print(f"Progress: {progress:.1f}% - Status: {status}")

                if status in ["completed", "failed", "aborted"]:
                    print(f"Job finished with status: {status}")
                    return status

                time.sleep(check_interval)
            except Exception as e:
                print(f"Error checking status: {e}")
                time.sleep(check_interval)

    def download_results(self, job_id):
        """Download job results."""
        print("Downloading results...")

        try:
            response = self._request_with_retry(
                "get",
                f"{self.base_url}/jobs/{job_id}/results"
            )

            if response.status_code != 200:
                print(f"Failed to get results: {response.status_code}")
                print(f"Response: {response.text}")
                return False

            results_data = response.json()

            # Create output folder
            self.output_folder.mkdir(parents=True, exist_ok=True)

            # Download images
            if "results" in results_data:
                for result in results_data["results"]:
                    if result.get("status") == "completed":
                        output_url = None
                        if result.get("output") and isinstance(result["output"], dict):
                            output_url = result["output"].get("full_size")

                        if output_url:
                            try:
                                img_response = requests.get(output_url, timeout=30)
                                img_response.raise_for_status()

                                instruction_index = result.get("group_index", result.get("image_index", 0))
                                variation_index = result.get("image_index", 0)
                                version = result.get("version", 0)
                                fmt = result.get("output_format", "jpg")
                                filename = f"output_{instruction_index}_{variation_index}_v{version}.{fmt}"

                                output_path = self.output_folder / filename
                                with open(output_path, "wb") as f:
                                    f.write(img_response.content)

                                model_used = result.get("model_used")
                                if model_used:
                                    print(f"Downloaded: {filename} (model: {model_used})")
                                else:
                                    print(f"Downloaded: {filename}")
                            except Exception as e:
                                print(f"Failed to download image {result.get('image_index')}: {e}")

            # Save metadata
            metadata_path = self.output_folder / "metadata.json"
            with open(metadata_path, "w") as f:
                json.dump(results_data, f, indent=2)

            print(f"Results saved to {self.output_folder}")
            return True
        except Exception as e:
            print(f"Error downloading results: {e}")
            return False

    def run(self):
        """Run the complete workflow."""
        print("=" * 70)
        print("Create Packshot")
        print("=" * 70)

        # Step 1: Upload garment images
        file_ids = self.upload_garment_images()
        if not file_ids:
            print("No images uploaded")
            return False

        if not self.project_id:
            print("No project ID available")
            return False

        # Step 2: Build instructions
        instructions = self._build_instructions()
        if instructions is None:
            return False

        # Step 3: Create job
        job_id = self.create_job(file_ids, instructions)
        if not job_id:
            return False

        # Step 4: Wait for completion
        status = self.wait_for_job(job_id)
        if status != "completed":
            print(f"Job did not complete successfully (status: {status})")
            return False

        # Step 5: Download results
        if not self.download_results(job_id):
            return False

        print("=" * 70)
        print("Processing complete")
        print("=" * 70)
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Script to generate clean product packshots from raw garment photos"
    )
    parser.add_argument(
        "--input-folder",
        type=str,
        required=True,
        help="Path to folder containing garment images (1-10 photos of the same product)"
    )
    parser.add_argument(
        "--output-folder",
        type=str,
        default="output",
        help="Output folder for results (default: output)"
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)"
    )
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token from https://app.on-model.com/profile?tab=tokens"
    )

    # Instruction flags (simple mode)
    instruction_group = parser.add_argument_group("instructions (simple mode)")
    instruction_group.add_argument(
        "--style",
        type=str,
        default="ghost_mannequin",
        choices=PACKSHOT_STYLES,
        help="Packshot style (default: ghost_mannequin). One of flat_lay, ghost_mannequin, "
             "marketing_ready, white_cutout, free."
    )
    instruction_group.add_argument(
        "--prompt",
        type=str,
        default=None,
        help="Free-form prompt overlay describing the desired output"
    )
    instruction_group.add_argument(
        "--background",
        type=str,
        default=None,
        help="Background description (e.g., 'white studio', 'concrete'). Style defaults apply if omitted."
    )
    instruction_group.add_argument(
        "--framing",
        type=str,
        default=None,
        choices=PACKSHOT_FRAMINGS,
        help="How the garment fills the canvas: square_packshot, tall_3_4, wide_4_3, full_frame."
    )
    instruction_group.add_argument(
        "--angle",
        type=str,
        default=None,
        choices=PACKSHOT_ANGLES,
        help="View angle: front, back, three_quarter, top_down, detail_macro. Forced to top_down for flat_lay."
    )
    instruction_group.add_argument(
        "--shadow",
        type=str,
        default=None,
        choices=PACKSHOT_SHADOWS,
        help="Shadow treatment: auto, none, contact, soft_drop, natural. Style-specific defaults apply."
    )
    instruction_group.add_argument(
        "--surface",
        type=str,
        default=None,
        help="Surface descriptor (e.g., 'linen', 'matte concrete'). Only meaningful for flat_lay style."
    )
    instruction_group.add_argument(
        "--num-variations",
        type=int,
        default=1,
        help="Number of output variations per instruction (1-8, default: 1)"
    )
    instruction_group.add_argument(
        "--size",
        type=str,
        default=None,
        choices=["1K", "2K", "4K"],
        help="Output resolution (default: server default)"
    )
    instruction_group.add_argument(
        "--aspect-ratio",
        type=str,
        default=None,
        choices=["1:1", "3:4", "4:3", "9:16", "16:9"],
        help="Output aspect ratio (default: server default)"
    )
    instruction_group.add_argument(
        "--format",
        type=str,
        default=None,
        dest="fmt",
        choices=["png", "jpg"],
        help="Output image format (default: jpg)"
    )
    instruction_group.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Seed value for reproducibility"
    )

    # Advanced mode
    advanced_group = parser.add_argument_group("instructions (advanced mode)")
    advanced_group.add_argument(
        "--instructions-file",
        type=str,
        default=None,
        help="Path to JSON file with instructions array (overrides all simple flags). "
             "Stack multiple styles in one job to cover several catalog surfaces from a single upload."
    )

    # Per-image annotations
    annotation_group = parser.add_argument_group("image annotations")
    annotation_group.add_argument(
        "--image-notes",
        type=str,
        nargs="+",
        default=None,
        help="Per-image notes in upload order (e.g., --image-notes 'front view' '' 'detail of lining'). "
             "Use empty string '' to skip an image. Notes highlight context the AI might otherwise miss."
    )

    # Job-level generation options
    generation_group = parser.add_argument_group("generation options")
    generation_group.add_argument(
        "--model",
        choices=["auto", "nano_banana_2", "nano_banana_pro", "seedream", "gpt_image"],
        default="auto",
        help="Generation engine: auto | nano_banana_2 | nano_banana_pro | seedream | gpt_image. "
             "'auto' (default) uses the default engine with a safety fallback. "
             "Specifying an engine disables the fallback."
    )
    generation_group.add_argument(
        "--use-anchor",
        action="store_true",
        default=False,
        help="Pin one instruction as the canonical look reference and align every output to it. "
             "Defaults to OFF for create-packshot (instructions usually intentionally diverge in style)."
    )
    generation_group.add_argument(
        "--anchor-index",
        type=int,
        default=0,
        help="Which instruction (zero-indexed) is used as the anchor when --use-anchor is set (default: 0)."
    )
    generation_group.add_argument(
        "--post-process",
        action="store_true",
        default=False,
        help="Enable automatic post-processing of results."
    )

    args = parser.parse_args()

    processor = CreatePackshot(
        base_url=args.base_url,
        token=args.token,
        input_folder=args.input_folder,
        output_folder=args.output_folder,
        style=args.style,
        prompt=args.prompt,
        background=args.background,
        framing=args.framing,
        angle=args.angle,
        shadow=args.shadow,
        surface=args.surface,
        num_variations=args.num_variations,
        size=args.size,
        aspect_ratio=args.aspect_ratio,
        fmt=args.fmt,
        seed=args.seed,
        instructions_file=args.instructions_file,
        image_notes=args.image_notes,
        model=args.model,
        use_anchor=args.use_anchor,
        anchor_index=args.anchor_index,
        post_process=args.post_process,
    )

    success = processor.run()

    if not success:
        exit(1)


if __name__ == "__main__":
    main()
