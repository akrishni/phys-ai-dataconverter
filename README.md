# phys-ai-dataconverter

Convert robot recordings into datasets you can train on. Version 0.1 turns ROS 2 teleoperation recordings
(MCAP) into [LeRobot](https://github.com/huggingface/lerobot) v3 datasets. It pairs camera frames, joint
state and the operator's commands by capture time, and refuses to fill gaps with stale values.

```bash
pip install phys-ai-dataconverter
phys-ai-dataconverter recordings/ -o ./pick_bottles --task "pick up the bottles" --check preset:teleop-imitation
```

Each `.mcap` file becomes one episode. `--check` then profiles the result's data table and videos with
[phys-ai-dataprofiler](https://github.com/akrishni/phys-ai-dataprofiler) and checks it against a
requirements file or profiler preset, saving the report in `meta/check.md`. The output works unchanged with LeRobot's `LeRobotDataset` and `lerobot-train`.

## What goes in

The camera's frames are the timeline, one row per frame:

| Column | From | Default |
|---|---|---|
| `observation.images.<camera>` | `sensor_msgs/CompressedImage` (JPEG, PNG) or raw `sensor_msgs/Image`, encoded to AV1 | the highest-resolution camera; `--camera` |
| `observation.state` | `sensor_msgs/JointState` positions | the joints the action commands; `--state-joints` |
| `action` | `trajectory_msgs/JointTrajectory` commands (the teleop leader), concatenated | every such topic; `--action-topic`, `--no-action` |

The camera name comes from the topic: `/zed/zed_node/left/image_rect_color/compressed` becomes
`observation.images.zed_left`, or set it with `--camera-key`. The topics are chosen from the first file and
printed, and every later file must have the same ones.

## Alignment

Each frame takes the joint reading and the command nearest to it in time, within half a frame interval
(`--tolerance-ms`). Leading or trailing frames with no match yet are dropped. A frame in the middle with no
match stops the conversion with the time and size of the gap. That's a gap in the recording, so the frame
isn't filled with a stale value. Raise `--tolerance-ms` to accept it.

**Clocks.** A ROS message has two times: when the sensor captured it (`header.stamp`) and when the
recorder wrote it (MCAP `log_time`). On real robots a camera frame reaches the recorder about 25 ms after
capture and a joint reading about 1 ms after, so pairing on write time matches each frame with the arm's
position a frame later. The converter pairs on capture time. Teleop commands usually carry no capture
time, so they're placed on the capture timeline using the joint topic's recorder delay. `--clock log`
pairs on write time; `--clock header` requires a capture time on every topic.

## What comes out

LeRobot v3.0, as written by lerobot 0.4 and later:

```
data/chunk-000/file-000.parquet            observation.state, action, timestamp, frame_index, episode_index, index, task_index
videos/<camera>/chunk-000/file-000.mp4     all episodes back to back, AV1, a keyframe every 2 frames
meta/info.json  stats.json  tasks.parquet  episodes/chunk-000/file-000.parquet
meta/conversion.json                       sources, topics, clock, worst match per episode, notes
```

`meta/conversion.json` also records what the converter noticed: dropped camera frames, a camera rate
that differs from the dataset fps, or a robot clock that isn't synchronised with the recorder (two of
the public test recordings below are 73 days off). The output is built in `<output>.partial` and renamed
when complete, so a failed run leaves nothing behind. `--overwrite` only ever replaces a folder that is
already a LeRobot dataset.

## Tested on

`ola31/readme_test_rosbag_1` and `Dongkkka/physical_ai_tools_test_rosbag` on the Hugging Face Hub, a
two-arm teleop robot: 3 recordings, 1,777 frames at 30 fps, 19 joints.
- **Values:** every state and action value equals the source message.
- **Timing:** each frame's joint reading is within 5 ms of the frame's capture time.
- **Video:** video frames are in source order.
- **LeRobot:** `LeRobotDataset` loads the result, and `lerobot-train` trains ACT on it.

## Limits

Version 0.1 handles one camera per dataset, ROS 2 MCAP input, and joint state and commands from
`JointState` and `JointTrajectory`. Planned next: several cameras, ROS 1 `.bag` and ROS 2 `.db3`.

## Development

```bash
git clone https://github.com/akrishni/phys-ai-dataconverter
cd phys-ai-dataconverter
pip install -e ".[dev]"
python tests/run_all.py            # offline: MCAP files with known answers are written by the tests
python tests/run_all.py --hub      # also convert a public recording from the Hub
```

```
phys_ai_dataconverter/
  model.py           Stream, image and topic types shared by everything below
  readers/           one module per input format (mcap_ros2)
  episode.py         one recording to one aligned episode: topic choice, clocks, alignment
  align.py           nearest-timestamp matching with a tolerance
  writers/           one module per output format (lerobot_v3)
  convert.py         the pipeline: read, align, write to <output>.partial, rename
  verify.py          --check: profile the output with phys-ai-dataprofiler and evaluate requirements
  cli.py             the phys-ai-dataconverter command
```

A new input format is a new module in `readers/` that produces `Stream`s. A new output format is a new
module in `writers/`. Nothing else changes.

## License

MIT License, copyright (c) 2026 Anand Krishniyer. See [LICENSE](LICENSE).
