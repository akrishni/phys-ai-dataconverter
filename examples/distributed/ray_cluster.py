#!/usr/bin/env python3
"""
Ray cluster distributed processing example.

Setup:
    pip install ray[default]
    ray start --head  # On head node
    ray start --address=<head-ip>:6379  # On worker nodes

Usage:
    python ray_cluster.py recordings/ dataset/
"""

import sys
from pathlib import Path

import ray

# Add converter to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from phys_ai_dataconverter import ConvertOptions, convert


@ray.remote
def convert_episode_batch(episode_files, output_base, options_dict):
    """Convert a batch of episodes on one Ray worker.

    Each batch is processed sequentially on the worker.
    Multiple batches run in parallel across workers.
    """
    options = ConvertOptions(**options_dict)

    results = []
    for ep_file in episode_files:
        try:
            # Create unique output for this episode
            output = Path(output_base) / f"episode_{Path(ep_file).stem}"
            report = convert([Path(ep_file)], output, options)

            results.append({
                'success': True,
                'file': ep_file,
                'frames': report.total_frames,
                'output': str(output)
            })
        except Exception as e:
            results.append({
                'success': False,
                'file': ep_file,
                'error': str(e)
            })

    return results


def chunk_list(items, chunk_size):
    """Split list into chunks"""
    for i in range(0, len(items), chunk_size):
        yield items[i:i + chunk_size]


def main(recordings_dir, output_dir, batch_size=10):
    """Process recordings with Ray cluster"""

    # Initialize Ray (connects to existing cluster if available)
    ray.init(address='auto', ignore_reinit_error=True)

    print(f"Ray cluster: {ray.cluster_resources()}")

    # Find all MCAP files
    recordings = Path(recordings_dir)
    files = sorted(str(f) for f in recordings.rglob("*.mcap"))

    print(f"\nFound {len(files)} episodes")
    print(f"Batch size: {batch_size} episodes per worker task")
    print(f"Total tasks: {(len(files) + batch_size - 1) // batch_size}")

    # Convert options to dict for serialization
    options_dict = {
        'task': 'teleoperation',
        'vcodec': 'libsvtav1',
        'crf': 30
    }

    # Submit batches to Ray cluster
    print("\nSubmitting batches...")
    batches = list(chunk_list(files, batch_size))
    futures = [
        convert_episode_batch.remote(batch, output_dir, options_dict)
        for batch in batches
    ]

    # Wait for all to complete
    print("Processing...")
    results = []
    for i, future in enumerate(futures):
        batch_results = ray.get(future)
        results.extend(batch_results)

        completed = (i + 1) * batch_size
        print(f"  Batch {i + 1}/{len(batches)} complete ({min(completed, len(files))}/{len(files)} episodes)")

    # Summary
    successful = sum(1 for r in results if r['success'])
    failed = len(results) - successful

    print(f"\n{'='*60}")
    print(f"Complete!")
    print(f"  Successful: {successful}")
    print(f"  Failed: {failed}")

    if failed > 0:
        print(f"\nFailed episodes:")
        for r in results:
            if not r['success']:
                print(f"  ✗ {r['file']}: {r['error']}")

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: python ray_cluster.py <recordings_dir> <output_dir> [batch_size]")
        sys.exit(1)

    recordings_dir = sys.argv[1]
    output_dir = sys.argv[2]
    batch_size = int(sys.argv[3]) if len(sys.argv) > 3 else 10

    sys.exit(main(recordings_dir, output_dir, batch_size))
