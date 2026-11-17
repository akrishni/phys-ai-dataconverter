"""
Parallel processing support for converting multiple episodes concurrently.

Usage:
    convert_parallel(files, output, options, workers=8, log=print)

Episodes are processed in separate processes using ProcessPoolExecutor. Each worker runs
a full episode conversion independently, allowing linear speedup on multi-core machines.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from phys_ai_dataconverter.convert import ConvertOptions, convert
from phys_ai_dataconverter.model import ConversionError


@dataclass
class ParallelProgress:
    """Progress tracking for parallel conversion"""
    total: int
    completed: int
    failed: int
    in_progress: int

    def percentage(self) -> float:
        return 100.0 * self.completed / self.total if self.total > 0 else 0.0


def convert_single_episode(episode_file: Path, output_dir: Path, options: ConvertOptions) -> dict:
    """Convert a single episode - designed to run in a separate process.

    Returns a dict with episode info for the main process to aggregate.
    """
    try:
        # Each worker creates its own temporary output directory
        episode_name = episode_file.stem
        temp_output = output_dir / f".partial_{episode_name}_{os.getpid()}"

        # Convert single episode
        report = convert([episode_file], temp_output, options, log=lambda m: None)

        return {
            'success': True,
            'file': str(episode_file),
            'temp_output': str(temp_output),
            'episode_index': len(report.episodes) - 1,
            'frames': report.total_frames,
            'report': report
        }
    except ConversionError as e:
        return {
            'success': False,
            'file': str(episode_file),
            'error': str(e)
        }
    except Exception as e:
        return {
            'success': False,
            'file': str(episode_file),
            'error': f"Unexpected error: {e}"
        }


def convert_parallel(files: list[Path], output: Path, options: ConvertOptions,
                     workers: int = 1, log=None) -> tuple[list[dict], list[dict]]:
    """Convert multiple episodes in parallel using ProcessPoolExecutor.

    Args:
        files: List of episode files to convert
        output: Output directory for the combined dataset
        options: Conversion options
        workers: Number of parallel workers (1 = sequential)
        log: Logging function

    Returns:
        (successful_results, failed_results)

    Each episode is converted to a temporary directory, then results are aggregated.
    Progress is reported as episodes complete.
    """
    log = log or (lambda m: None)

    if workers == 1:
        # Sequential processing (existing behavior)
        log(f"Converting {len(files)} episodes sequentially...")
        successful = []
        failed = []

        for i, file in enumerate(files):
            log(f"Episode {i + 1}/{len(files)}: {file.name}")
            result = convert_single_episode(file, output, options)
            if result['success']:
                successful.append(result)
            else:
                failed.append(result)

        return successful, failed

    # Parallel processing
    log(f"Converting {len(files)} episodes with {workers} workers...")

    successful = []
    failed = []
    progress = ParallelProgress(total=len(files), completed=0, failed=0, in_progress=0)

    with ProcessPoolExecutor(max_workers=workers) as executor:
        # Submit all jobs
        future_to_file = {
            executor.submit(convert_single_episode, file, output, options): file
            for file in files
        }

        # Process results as they complete
        for future in as_completed(future_to_file):
            file = future_to_file[future]

            try:
                result = future.result()

                if result['success']:
                    successful.append(result)
                    progress.completed += 1
                    log(f"✓ {file.name} ({progress.completed}/{progress.total}, "
                        f"{progress.percentage():.1f}%)")
                else:
                    failed.append(result)
                    progress.failed += 1
                    log(f"✗ {file.name}: {result['error']}")

            except Exception as e:
                # Shouldn't happen (errors caught in worker), but handle anyway
                failed.append({
                    'success': False,
                    'file': str(file),
                    'error': f"Worker exception: {e}"
                })
                progress.failed += 1
                log(f"✗ {file.name}: Worker exception: {e}")

    log(f"\nCompleted: {progress.completed} successful, {progress.failed} failed")
    return successful, failed


def merge_episode_results(successful_results: list[dict], output: Path, options: ConvertOptions, log=None):
    """Merge individual episode outputs into a single dataset.

    This is called after parallel processing to combine all the temporary episode
    outputs into one coherent LeRobot v3 dataset.

    For now, this is a placeholder - the actual implementation would:
    1. Combine all data parquet files
    2. Concatenate all videos
    3. Merge metadata (info.json, stats.json, episodes table)
    4. Clean up temporary directories

    TODO: Implement full merging logic
    """
    log = log or (lambda m: None)

    log("\nMerging results...")
    log("Note: Full parallel merge not yet implemented.")
    log("For now, use sequential processing or manual episode combination.")

    # Placeholder: list what needs to be merged
    for result in successful_results:
        log(f"  - {result['temp_output']} ({result['frames']} frames)")

    raise NotImplementedError(
        "Parallel processing merge not yet implemented. "
        "Use --workers 1 for sequential processing, or see DISTRIBUTED_PROCESSING.md "
        "for manual episode sharding approaches."
    )
