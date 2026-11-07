# Launch trailer script (88 seconds)

A product-launch trailer: black stage, one idea per beat, big type, long holds, then a hard hit. The god of
sleep is the hero image; everything about the product is real motion graphics built from the GPU run in the
README. The caption "Synthetic demo data · real training runs" stays on screen the whole time.

- Built with HyperFrames (motion graphics and the edit), Higgsfield (images and three image-to-video shots of
  the god) and ElevenLabs (narration in the voice "Orion", and the score with ElevenLabs Music).
- Type: Newsreader, a serif, for statements, the title and the big numbers; Geist Mono for data, labels and
  terminal lines. The god speaks in serif, the machine in mono.
- Color: night black, moonlight white, Hypnos lavender (REM), stage blue (N3), dawn gold, promotion green and
  nightmare red, the colors of the Desktop plugin.
- Project: `../hypnos-trailer/`, outside this repo so media never lands here.

## Rhythm

`cold open (slow) → REVEAL (shader) → day (quick) → NIGHT (slow centerpiece) → principle (split) → exam
(punch) → PROOF (peak) → trust (beat) → end card (hold)`

## Beats

| Time | Beat | Picture and motion | Voice |
|---|---|---|---|
| 0.0 to 7.0 | The loop | Black. A mono chat bubble types on: "no, we use pnpm here", stamped Mon 09:12. A second one, Tue 10:40. A third, in Indonesian, Wed 14:03: "bukan npm, pakai pnpm". The bubbles stack; a counter ticks to ×3. | "You told it to use pnpm." ... "Then you told it again. And again." |
| 7.0 to 12.5 | Text is not learning | A `MEMORY.md` card fills with the same line, again and again, faster, until it scrolls away. The lines grey out. | "Agents write down what you say. They never learn how you work." |
| 12.5 to 21.0 | Reveal | Dissolve into the god asleep under a crescent moon (Higgsfield shot: slow push, drifting mist). A soft boom. HYPNOS draws on in thin serif with wide tracking; the tagline fades up beneath. | "This is Hypnos." ... "Hermes learns in its sleep." |
| 21.0 to 31.0 | Day | Signal chips fly in from the edges: approved, denied, corrected, tests passed. Each drops into a moon gauge that fills to 78% and reads "Sleep pressure". | "By day, it watches. What you approve. What you deny. What you correct." |
| 31.0 to 47.0 | Night, the centerpiece | Over the dream nebula, the real hypnogram of night 2 draws itself: WAKE, N1, N2, then four cycles of N3 and REM. Labels land: "N3 · replay the day", "REM · dream the mistakes". Red nightmare diamonds pop, then turn green. A sandbox line: `rm -rf ../app-backup/dist` struck out, `rm -rf ./dist` checked. The lattice shot (Higgsfield) blooms behind "a small brain of its own". | "At night, it sleeps." ... "It replays the day. It dreams up new versions of its mistakes, practises them in a sandbox, and trains what it learned into a small brain of its own." |
| 47.0 to 54.0 | The principle | Split frame. Left, "Habits → weights": "Use pnpm, never npm" flies into the lattice and dissolves into light. Right, "Facts → memory": "The API runs on Postgres 16" stays put in `MEMORY.md`. | "Habits go into the weights. Facts stay in memory." |
| 54.0 to 63.0 | The exam | The god wakes at dawn (Higgsfield shot: gold light, eyes open). Exam bars race: held-out next action 67% → 83%, habit probes 70% → 78%, dreams 60% → 80%. Stamp: PROMOTED · BRAIN V2. Then a quick second card: NIGHT 4 · REJECTED · V2 STAYS. | "In the morning, it takes an exam. Pass, and the new brain takes over. Fail, and the old one stays." |
| 63.0 to 75.0 | Proof, the peak | The keynote number: 24,636 counts down to 5,256 prompt tokens per turn. Five day bars fall in sequence. Then two more figures: 81% of turns answered by its own brain; 100% task success on days 2 to 5. | "By day five, its prompt is a fifth the size. And the work is still right." |
| 75.0 to 81.0 | Trust | A Hermes approval card: the brain proposes `rm -rf ./dist`, with Allow once and Deny. The choice waits for you. | "Every command still asks you first." |
| 81.0 to 88.0 | End card | The god in profile over the poppy field. HYPNOS. "Hermes learns in its sleep." "A Hermes Agent plugin · github.com/0xNoramiya/hypnos". Fade to black. | "Hypnos. Hermes learns in its sleep." |

## Numbers on screen

All from the five-day GPU timelapse in the README (Qwen3-0.6B on one A40, synthetic persona):

- Prompt tokens per turn: 24,636 on day 1, 5,256 on day 5 (21%, "a fifth").
- Turns answered by the brain on day 5: 81%. Task success: 76% on day 1, 100% on days 2 to 5.
- Night 2 exam against v1: held-out 67% → 83%, probes 70% → 78%, dreams 60% → 80%; promoted v2.
- Night 4: rejected, v2 stayed.
- Sleep pressure after day 1: 78%.

A rerun trains differently. If you rebuild the trailer from another run, take the numbers from its report.

## Recording the live product instead

To show the real Desktop rather than motion graphics, use the `hypnos-demo` profile, which holds the GPU run:

1. `hermes -p hypnos-demo hypnos serve start` to load brain v2, and give the profile a model for the turns
   the brain does not handle.
2. In Hermes Desktop, connect to `hypnos-demo` and turn on Hypnos under Capabilities, then Plugins.
3. Record `/hypnos` (pick night 2 in the hypnogram), then a chat turn such as "Add dayjs to the CLI." with its
   reply badge, at 1080p in the dark theme. Keep the "synthetic demo data" caption on screen.

Post on X with the trailer, tag @NousResearch, and link the repository.
