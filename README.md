<div align="center">

# 🎛️ rigsmith

**Autonomous AI Tone Engineering & Smart Remote Control for HeadRush Core / Prime**

*Recreate any recorded guitar or bass tone on your real hardware amp modeler over Wi-Fi — automated, measured, and verified.*

[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm%20Noncommercial-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-brightgreen.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-teal.svg)](https://fastapi.tiangolo.com)
[![Tests Passing](https://img.shields.io/badge/tests-1170%2B%20passing-success.svg)](tests/)

[Features](#-features) • [Quickstart](#-quickstart) • [Architecture](#-architecture) • [Workflow](#-the-end-to-end-workflow) • [Safety](#-hardware-safety-first) • [License](#-license)

</div>

---

## ⚡ What is Rigsmith?

**Rigsmith** turns your **HeadRush Core / Prime** into an autonomous tone engineering lab. It bridges the gap between a song you love and your modeler's real rig chain.

Instead of spending hours manually guessing amp models, cab IRs, drive settings, and EQ curves, Rigsmith:
1. **Analyzes songs & stems**: Isolates the guitar/bass layers, transcribes chords, detects tuning, and measures spectral & dynamic fingerprints.
2. **Researches historic gear recipes**: Identifies what amps, cabs, mics, and pickups were originally used on the recording.
3. **Learns your specific guitar & bass**: Uses a clicked, interactive lab with built-in chromatic/string tuning (down to 25 Hz), gain staging coaching, and voice modeling.
4. **Optimizes your hardware over Wi-Fi**: Controls your HeadRush unit via its native Remote Editor protocol, sweeping candidate amps/cabs/NAM captures, reamping test takes through your audio interface, and matching harmonic profiles until the ESR error is minimized.
5. **Gives you the final say**: Generates automated blind A/B trials and exports full multi-scene song rigs directly to your device.

Available as a **Modern Dark Web UI** (`http://127.0.0.1:8765`), a full **CLI**, and a standard **MCP Server** for Claude, Gemini, and local LLM agents.

---

## ✨ Features

- **🚀 Autopilot Mode**: Drop in an audio track or pick a song title. Rigsmith analyzes the track, drafts the rig, matches the tone, runs blind listening tests, and outputs a ready-to-gig rig.
- **🎸 Guitar & Bass Lab (Step 1 Pre-Flight)**:
  - Real-time chromatic tuner supporting standard guitars and 4/5/6-string basses down to 25 Hz ($A_0$, $B_0$, $E_1$) with needle gauge and audio reference pitches.
  - Interactive dBFS gain staging meter with a dedicated **-18 to -6 dBFS sweet-spot coach** for your audio interface knob.
  - Zero-latency duplex live audio pass-through monitoring + instant check-clip recording and browser playback.
  - Guided session protocols that map your fretboard coverage and fit a harmonic voice model.
- **🔊 Hardware Loop & NAM Integration**:
  - Sample-aligned round-trip reamping across analog, USB, and hybrid interface loops.
  - Native integration with Neural Amp Modeler (NAM A1/A2) and IR impulse responses.
  - Automated knob-turning search across gain, EQ, presence, and master controls.
- **🛡️ Rock-Solid Device Safety**:
  - Zero firmware flashing or modification. Operates strictly over HeadRush's standard Wi-Fi Remote Editor protocol.
  - Strict **Validate → Backup → Diff → Confirm → Write → Verify** pipeline. Factory rigs are never overwritten without an atomic local backup.
- **🤖 Built-in Agentic AI & Command Palette**:
  - Full MCP tool interface (`mcp_rigsmith_*`) allowing LLMs (Claude Code, Antigravity, LM Studio, Ollama) to drive tone research and rig building.
  - Global `Ctrl+K` command palette to navigate anywhere instantly.

---

## 🚀 Quickstart

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/Gavinboley/rigsmith.git
cd rigsmith

# Install in editable mode with all DSP dependencies
pip install -e ".[all]"

# Check environment, audio devices, and audio interface setup
rigsmith doctor
```

### 2. Launch the Web UI

```bash
rigsmith ui
```
Open **`http://127.0.0.1:8765`** in your browser to access the control surface.

### 3. Connect Your HeadRush Core / Prime

1. Connect your HeadRush Core/Prime to the same local Wi-Fi network as your computer.
2. On your HeadRush screen, enable **Global Settings → Wi-Fi → HeadRush Remote**.
3. In your terminal or the Web UI:
   ```bash
   rigsmith discover              # Scan local subnet for your device
   rigsmith device pair           # Confirm the pairing PIN on the device screen
   ```

---

## 🔄 The End-to-End Workflow

```mermaid
flowchart LR
    A["Song / Stem"] --> B["Song Mapper & Stem Separation"]
    B --> C["Tone Research & Recipe Engine"]
    C --> D["Autopilot Search & NAM Shortlist"]
    D --> E["Hardware Loop Reamp & Metric Tuning"]
    E --> F["Blind A/B Hearing Test"]
    F --> G["HeadRush Wi-Fi Write & Backup"]
    
    H["Your Guitar / Bass"] --> I["Guitar Lab: Tuner & Gain Staging"]
    I --> D
```

1. **Intake & Separation**: Demucs splits guitar, bass, vocal, and drum stems. Pitch and chord transcribers extract exact rhythms and voicings.
2. **Instrument Profiling**: Plug into your audio interface (e.g., E2x2, Scarlett, Volt), dial your gain knob into the `-18 to -6 dBFS` sweet spot, tune up, and play a 1-minute guided chromatic sequence.
3. **Tone Matching**: Rigsmith compares your reamped DI against the isolated song stem, calculating envelope-aligned ESR (Error-to-Signal Ratio) and spectral balance.
4. **Hardware Sync**: The resulting rig chain, complete with calibrated scenes, delay divisions, and custom NAM captures, is written straight to your HeadRush unit over Wi-Fi.

---

## 🧰 CLI Cheat Sheet

| Task | Command |
|---|---|
| Launch Web UI | `rigsmith ui` |
| Health Check | `rigsmith doctor` |
| Device Discovery | `rigsmith discover` |
| Run Autopilot Demo | `rigsmith autopilot demo` |
| Autopilot on a Song | `rigsmith autopilot run "Song Title" --artist "Artist" --mix track.flac` |
| Test Hardware Loop Wiring | `rigsmith loop test` |
| Capture Audio Take | `rigsmith loop record 10 --device "Analog 1/2"` |
| Inspect Rig JSON | `rigsmith rig show "MyRig"` |

---

## 🛡️ Hardware Safety First

Rigsmith treats your hardware with care:
- **Loopback Default**: Web UI binds exclusively to `127.0.0.1` with DNS rebinding protection and strict HTTP Origin validation.
- **Mandatory Backup Gate**: Rig writes fail closed unless an atomic backup of the device state exists in `~/.rigsmith/backups`.
- **Read-Only Mode**: Run `RIGSMITH_READONLY=1 rigsmith ui` to lock all hardware write mutations during live shows or rehearsal.
- **Zero Risk Recovery**: Powering on the HeadRush unit while holding footswitches **4 + 5** boots into safe factory restore at any time.

---

## 📜 License

This project is licensed under the **PolyForm Noncommercial License 1.0.0**.

- ✅ **Free for personal use, hobbyist experimentation, research, and educational study.**
- ✅ **You may fork, modify, and distribute with prominent attribution to Gavin Boley.**
- ❌ **Commercial use, selling the software, hosting it as a paid service, or redistributing it for commercial advantage is strictly prohibited.**

See the full [LICENSE](LICENSE) file for complete legal terms.

---

<div align="center">
  <sub>Crafted by <strong><a href="https://github.com/Gavinboley/rigsmith">Gavin Boley</a></strong>. Powered by open-source audio DSP and modern AI.</sub>
</div>
