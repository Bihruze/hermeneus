# Submission form draft

Status: DRAFT for Seher's approval. The field list is a guess at a typical hackathon form; check it against the real form. URLs are placeholders.

| Field | Draft answer |
| --- | --- |
| Project name | Hermeneus |
| Tagline | Live translation for Hermes |
| Short description | Hermeneus listens to audio playing on a Mac and shows a translated subtitle for each line of speech, in a Hermes Desktop pane and in the terminal. It can also speak the translation in your headphones. |
| Long description | Hermeneus captures one app or all system audio with a Core Audio process tap, so it needs no audio driver on macOS 14.2 and later. ElevenLabs Scribe v3 Live transcribes the speech with speaker labels. Each finished line is translated by the model you already use in Hermes, with the last few lines as context so names stay consistent. Subtitles appear in a Desktop pane, in `hermes hermeneus run`, and through `/translate status`. With spoken translation on, ElevenLabs Flash v2.5 reads each line on the output device you pick. `hermes hermeneus doctor` checks what the Mac needs and gives a fix for each gap. |
| How it uses Hermes | It is a Hermes plugin: a CLI command group, the `/translate` slash command, a Desktop pane with backend routes on the Hermes gateway, and translation through the plugin LLM API under the `live_translate` auxiliary task. |
| Platforms | macOS 14.2 or later. macOS 13 works with a virtual loopback device. |
| Repository URL | https://github.com/Bihruze/hermeneus |
| Demo video URL | `<DEMO_VIDEO_URL>` |
| Plugin catalog PR URL | `<CATALOG_PR_URL>` |
| Banner image | `<BANNER_RAW_URL>` (2:1, 1200x600, pinned to the release commit) |
| Team | `<TEAM_FIELD>` |
| License | MIT |
