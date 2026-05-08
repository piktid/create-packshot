#!/usr/bin/env python3
"""
Batch Create Packshot - Process multiple garment folders in parallel.

This script wraps create_packshot.py to process multiple garment folders concurrently,
with a configurable parallelism level (default: 3, max: 5).

Each parallel worker runs a full independent workflow (auth, upload, job, download)
so there is no shared state between threads.

Usage:
    # Process all subfolders in a directory
    python batch_create_packshot.py \
        --input-dir garments/ \
        --token YOUR_API_TOKEN \
        --output-dir results/

    # Process specific folders with stacked instructions
    python batch_create_packshot.py \
        --input-folders garments/BLAZER1 garments/BLAZER2 garments/BLAZER3 \
        --token YOUR_API_TOKEN \
        --instructions-file example_instructions.json \
        --output-dir results/ \
        --parallel 5
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from create_packshot import (
    CreatePackshot,
    PACKSHOT_ANGLES,
    PACKSHOT_FRAMINGS,
    PACKSHOT_SHADOWS,
    PACKSHOT_STYLES,
)


def process_single_garment(base_url, token, input_folder, output_folder,
                           style, prompt, background, framing, angle, shadow, surface,
                           num_variations, size, aspect_ratio, fmt, seed,
                           instructions_file, image_notes,
                           model, use_anchor, anchor_index, post_process):
    """Process a single garment folder. Runs in its own thread with its own CreatePackshot instance."""
    start = time.time()

    processor = CreatePackshot(
        base_url=base_url,
        token=token,
        input_folder=str(input_folder),
        output_folder=str(output_folder),
        style=style,
        prompt=prompt,
        background=background,
        framing=framing,
        angle=angle,
        shadow=shadow,
        surface=surface,
        num_variations=num_variations,
        size=size,
        aspect_ratio=aspect_ratio,
        fmt=fmt,
        seed=seed,
        instructions_file=instructions_file,
        image_notes=image_notes,
        model=model,
        use_anchor=use_anchor,
        anchor_index=anchor_index,
        post_process=post_process,
    )

    success = processor.run()
    elapsed = time.time() - start

    return {
        "folder": input_folder.name,
        "success": success,
        "processing_time": round(elapsed, 1),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Batch Create Packshot - Process multiple garment folders in parallel"
    )

    # Input: either --input-dir (all subfolders) or --input-folders (specific paths)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--input-dir",
        type=str,
        help="Directory containing garment subfolders (each subfolder is processed as a separate job)",
    )
    input_group.add_argument(
        "--input-folders",
        type=str,
        nargs="+",
        help="Specific garment folder paths to process",
    )

    parser.add_argument(
        "--token", type=str, required=True, help="API token from https://app.on-model.com/profile?tab=tokens"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="output",
        help="Base output directory — results saved to <output-dir>/<folder-name>/ (default: output)",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=3,
        help="Number of parallel workers (default: 3, max: 5)",
    )

    # Instruction flags (simple mode)
    instruction_group = parser.add_argument_group("instructions (simple mode)")
    instruction_group.add_argument(
        "--style",
        type=str,
        default="ghost_mannequin",
        choices=PACKSHOT_STYLES,
        help="Packshot style (default: ghost_mannequin)",
    )
    instruction_group.add_argument(
        "--prompt", type=str, default=None, help="Free-form prompt overlay"
    )
    instruction_group.add_argument(
        "--background", type=str, default=None, help="Background description"
    )
    instruction_group.add_argument(
        "--framing", type=str, default=None, choices=PACKSHOT_FRAMINGS, help="Garment framing"
    )
    instruction_group.add_argument(
        "--angle", type=str, default=None, choices=PACKSHOT_ANGLES, help="View angle"
    )
    instruction_group.add_argument(
        "--shadow", type=str, default=None, choices=PACKSHOT_SHADOWS, help="Shadow treatment"
    )
    instruction_group.add_argument(
        "--surface", type=str, default=None, help="Surface (flat_lay style only)"
    )
    instruction_group.add_argument(
        "--num-variations", type=int, default=1, help="Number of output variations (1-8, default: 1)"
    )
    instruction_group.add_argument(
        "--size", type=str, default=None, choices=["1K", "2K", "4K"], help="Output resolution"
    )
    instruction_group.add_argument(
        "--aspect-ratio", type=str, default=None, choices=["1:1", "3:4", "4:3", "9:16", "16:9"],
        help="Output aspect ratio"
    )
    instruction_group.add_argument(
        "--format", type=str, default=None, dest="fmt", choices=["png", "jpg"], help="Output format"
    )
    instruction_group.add_argument(
        "--seed", type=int, default=None, help="Seed value for reproducibility"
    )

    # Advanced mode
    advanced_group = parser.add_argument_group("instructions (advanced mode)")
    advanced_group.add_argument(
        "--instructions-file", type=str, default=None,
        help="Path to JSON file with instructions array (overrides all simple flags)"
    )

    # Per-image annotations
    annotation_group = parser.add_argument_group("image annotations")
    annotation_group.add_argument(
        "--image-notes",
        type=str,
        nargs="+",
        default=None,
        help="Per-image notes in upload order. Same syntax as create_packshot.py."
    )

    # Job-level generation options
    generation_group = parser.add_argument_group("generation options")
    generation_group.add_argument(
        "--model",
        choices=["auto", "nano_banana_pro", "seedream"],
        default="auto",
        help="Generation engine. 'auto' (default) uses the default engine with safety fallback.",
    )
    generation_group.add_argument(
        "--use-anchor",
        action="store_true",
        default=False,
        help="Anchor outputs to one instruction. Defaults to OFF for create-packshot.",
    )
    generation_group.add_argument(
        "--anchor-index",
        type=int,
        default=0,
        help="Which instruction is used as the anchor when --use-anchor is set (default: 0)",
    )
    generation_group.add_argument(
        "--post-process",
        action="store_true",
        default=False,
        help="Enable automatic post-processing.",
    )

    args = parser.parse_args()

    # Cap parallelism at 5 to respect API rate limits
    parallel = max(1, min(args.parallel, 5))

    # Collect input folders
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.exists():
            print(f"Input directory not found: {input_dir}")
            exit(1)
        folders = sorted([f for f in input_dir.iterdir() if f.is_dir()])
    else:
        folders = [Path(f) for f in args.input_folders]
        missing = [f for f in folders if not f.exists()]
        if missing:
            for f in missing:
                print(f"Folder not found: {f}")
            exit(1)

    if not folders:
        print("No folders to process")
        exit(1)

    output_dir = Path(args.output_dir)

    print("=" * 70)
    print("Batch Create Packshot")
    print("=" * 70)
    print(f"  Folders to process: {len(folders)}")
    print(f"  Parallel workers:   {parallel}")
    print(f"  Output directory:   {output_dir}")
    print(f"  Model:              {args.model}")
    print(f"  Style:              {args.style}")
    print(f"  Anchor:             {'on (index ' + str(args.anchor_index) + ')' if args.use_anchor else 'off'}")
    print(f"  API base URL:       {args.base_url}")
    if args.instructions_file:
        print(f"  Instructions file:  {args.instructions_file}")
    print("=" * 70)

    for i, folder in enumerate(folders, 1):
        print(f"  {i}. {folder.name}")
    print()

    start_time = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        future_to_folder = {}

        for folder in folders:
            per_folder_output = output_dir / folder.name
            future = executor.submit(
                process_single_garment,
                args.base_url,
                args.token,
                folder,
                per_folder_output,
                args.style,
                args.prompt,
                args.background,
                args.framing,
                args.angle,
                args.shadow,
                args.surface,
                args.num_variations,
                args.size,
                args.aspect_ratio,
                args.fmt,
                args.seed,
                args.instructions_file,
                args.image_notes,
                args.model,
                args.use_anchor,
                args.anchor_index,
                args.post_process,
            )
            future_to_folder[future] = folder.name

        for future in as_completed(future_to_folder):
            folder_name = future_to_folder[future]
            try:
                result = future.result()
                results.append(result)
                status = "OK" if result["success"] else "FAILED"
                print(
                    f"\n[{len(results)}/{len(folders)}] {folder_name}: {status}"
                    f" ({result['processing_time']}s)"
                )
            except Exception as e:
                results.append({"folder": folder_name, "success": False, "error": str(e)})
                print(f"\n[{len(results)}/{len(folders)}] {folder_name}: ERROR - {e}")

    # Summary
    elapsed = time.time() - start_time
    successful = sum(1 for r in results if r["success"])
    failed = len(results) - successful

    print(f"\n{'=' * 70}")
    print("Summary")
    print(f"{'=' * 70}")
    print(f"  Total:      {len(results)}")
    print(f"  Successful: {successful}")
    print(f"  Failed:     {failed}")
    print(f"  Time:       {elapsed:.1f}s ({elapsed / 60:.1f} minutes)")

    if failed > 0:
        print(f"\n  Failed folders:")
        for r in results:
            if not r["success"]:
                error = r.get("error", "see console output above")
                print(f"    - {r['folder']}: {error}")

    print(f"{'=' * 70}")

    # Save batch summary
    summary_file = output_dir / f"batch_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(summary_file, "w") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "configuration": {
                    "parallel": parallel,
                    "model": args.model,
                    "style": args.style,
                    "use_anchor": args.use_anchor,
                    "anchor_index": args.anchor_index,
                    "post_process": args.post_process,
                    "base_url": args.base_url,
                    "instructions_file": args.instructions_file,
                },
                "total_folders": len(results),
                "successful": successful,
                "failed": failed,
                "total_time_seconds": round(elapsed, 1),
                "results": results,
            },
            f,
            indent=2,
        )
    print(f"Batch summary saved to {summary_file}")

    if failed > 0:
        exit(1)


if __name__ == "__main__":
    main()
