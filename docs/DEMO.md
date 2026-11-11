# Demo script (draft, under 90 seconds)

Status: DRAFT for Seher's approval. Every command below exists on `main` (README.md, `hermeneus_engine/options.py:37-45`, `commands.py:25`). Items marked PENDING depend on work that is not on `main`.

## Before recording

- Hermes Desktop open, Hermeneus pane visible at the bottom, gateway restarted after `hermes plugins enable hermeneus`.
- `hermes hermeneus doctor` shows `ok` on every line. The System Audio Recording permission is already granted to Hermes and to the terminal app, so no prompt appears on camera.
- Headphones connected. Run `hermes hermeneus devices` and note the exact headphone name for `--device`.
- Browser tab 1: a Discord voice channel or a Twitch stream with English speech. Tab 2: the second platform (a Twitch stream, a YouTube talk, or a Google Meet call).
- A screen recorder that also records the headphone output, so viewers hear the spoken translation.
- The fallback clip (see below) ready to play.

## Shot list

| Time | On screen | Action or command | Narration |
| --- | --- | --- | --- |
| 0:00 to 0:08 | Banner, then Hermes Desktop | none | "Hermeneus gives Hermes live translation for any audio on your Mac." |
| 0:08 to 0:18 | Terminal | `hermes hermeneus doctor` | "One check shows what this Mac needs. Everything is ready." |
| 0:18 to 0:35 | Browser tab 1 (Discord or Twitch), pane below it | In the pane: source Google Chrome, language Turkish, press Start. Same as `/translate start --to tr --source chrome`. | "I pick the browser and Turkish, then press Start. Each English line gets a Turkish subtitle with its speaker." |
| 0:35 to 0:50 | Browser tab 2, pane keeps running | Switch tabs. No restart. | "Another platform, same session. Hermeneus listens to the app, not to one website." |
| 0:50 to 0:58 | Terminal beside the pane | `hermes hermeneus run --to tr` (attaches to the running session) | "The same lines also show in the terminal." |
| 0:58 to 1:15 | Pane with Speak translation on | Stop, tick Speak translation, pick the headphones, Start. CLI form: `hermes hermeneus run --to tr --source chrome --speak --device "<headphones>"` | "Now Hermes reads the translation aloud in my headphones." |
| 1:15 to 1:25 | Pane, then terminal | `hermes hermeneus stop` | "Stop ends the session. Install it from the Hermes plugin catalog." |
| 1:25 to 1:28 | Banner with repo URL | none | none |

Total: 88 seconds.

## Fallback if a live source is silent

1. Keep a local English clip (a talk you have the right to use) in a QuickTime Player or browser tab before recording.
2. If tab 1 or tab 2 is silent for more than 3 seconds, switch to the clip and change the source: `/translate start --to tr --source "QuickTime Player"` or pick it in the pane.
3. If the network to ElevenLabs drops, cut that take and re-record the segment. Do not show an error state as part of the demo.
4. Record each shot as its own take so one silent source costs one shot, not the whole video.

## PENDING

- Demo video URL: not recorded yet.
- Catalog listing: `hermes plugins install hermeneus` works only after the catalog PR is merged. Until then show `hermes plugins install Bihruze/hermeneus`.
- Banner: three drafts await Seher's choice.
