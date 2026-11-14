"""
Output formats. A writer module provides `is_dataset(path)`, `video_path(root, camera_key)`, a
`VideoWriter`, and `write(root, ...)` for everything else; see lerobot_v3 for the reference.
"""

from phys_ai_dataconvertor.writers import lerobot_v3

WRITERS = {"lerobot-v3": lerobot_v3}
