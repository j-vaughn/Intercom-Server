# Intercom-Server


This project is a web-based control panel for managing audio announcements across a fleet of networked intercom devices (such as Raspberry Pi-based endpoints). It provides a dashboard for uploading and managing audio files, organizing intercoms into zones, sequencing announcement sounds, and triggering scheduled or ad hoc announcements across selected devices.

---

## Key Features

- **Intercom Management**: Add, edit, group, or disable intercom devices.
- **Sound File Management**:
  - Upload `.wav` files
  - Automatically detect and store duration
  - Sync uploaded sounds to all remote intercoms (via SSH/SFTP)
- **Group Management**: Organize intercoms into named groups for targeting.
- **Announcements**:
  - Create named announcement sequences using uploaded sounds
  - Set volume modifiers and playback order
- **Saved Commands**:
  - Create reusable triggers for a single sound or announcement on a device or group
  - Configure repeat count and looping
- **Saved Command Sets**:
  - Combine multiple saved commands into a single named trigger
  - Useful for scenarios like fire alarms or coordinated multi-zone messages
- **Global Controls**:
  - Stop all playback across all intercoms
  - Clear the command queue
- **Intercom Status View**: See online/offline status of all intercoms and their current playback state
- **Queue Processor**:
  - Handles queued commands in background
  - Respects `start_time` for synchronized playback using NTP

---

## System Architecture

- **Server**: Python Flask app (with SQLite DB) running on central controller
- **Clients**: Intercoms (e.g. Raspberry Pis) listening on port `8084` for HTTP requests
- **Audio Sync**: `start_time` values are passed to intercoms, which must be NTP-synced

---

## Object Overview

### `Intercom`
Represents a device (e.g. Raspberry Pi) that can play audio.

- `id`
- `name`
- `ip_address`
- `volume_modifier`
- `disabled` (bool, skips device in all logic if set)

### `IntercomGroup`
Named group of intercoms.

- `id`
- `name`
- Relationship to `Intercom` via `GroupMembership`

### `Sound`
Uploaded `.wav` file.

- `id`
- `name`
- `filename`
- `play_duration_ms`
- `volume_modifier`

### `Announcement`
Ordered list of sound IDs to play as a sequence.

- `id`
- `name`
- `volume_modifier`
- `sound_order` (CSV of Sound IDs)

### `SavedCommand`
One logical playback operation (announcement or sound, target + volume + repetitions).

- `id`
- `name`
- `intercom_id` or `intercom_group_id`
- `sound_id` or `announcement_id`
- `volume_modifier`
- `times_to_play`
- `loop_forever`

### `SavedCommandSet`
Groups multiple SavedCommands into one named object.

- `id`
- `name`
- many-to-many relationship to `SavedCommand` via `SavedCommandSetMembership`

### `AnnouncementCommand`
Active commands in the queue (instantiated from SavedCommands).

---

## Usage

### Running the Server

```bash
python3 server.py
```

Access the Web UI
Visit: http://<server-ip>:8000

## Notes

- Intercoms marked disabled are ignored in all playback
- Commands are dispatched with start times to allow sync
- Queue processing is resilient to failed devices and continues execution
- Announcement queue and looped playback can be cleared using the "Stop All & Clear Queue" feature
