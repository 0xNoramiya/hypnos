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
- Project: `../hypnos-trailer/` (`index.html`, `design.md`), outside this repo so media never lands here.
  Render with `npx hyperframes render` there; the mix (`assets/mix.wav`) has the narration placed on these
  beats and the score ducked under it, at -14 LUFS.

## Rhythm

`cold open (slow) → REVEAL (shader) → day (quick) → NIGHT (slow centerpiece) → principle (split) → exam
(punch) → PROOF (peak) → trust (beat) → end card (hold)`

## Beats

| Time | Beat | Picture and motion | Voice |
|---|---|---|---|
| 0.0 to 7.0 | The loop | Black, a felt-piano note. A mono chat bubble types on: "no, we use pnpm here", stamped MON 09:12. A second one, TUE 10:40. A third, in Indonesian, WED 14:03: "bukan npm, pakai pnpm". A counter turns ×1, ×2, ×3, the last in red. | "You told it to use pnpm." ... "Then you told it again. And again." |
| 7.0 to 12.0 | Text is not learning | A `MEMORY.md` card fills with the same line, faster and faster, and scrolls away; the lines grey out. Beside it: "Written down." then, in lavender italic, "Never learned." | "Agents write down what you say. They never learn how you work." |
| 12.0 to 23.0 | Reveal | A dip to black, then the god asleep under a crescent moon (Higgsfield shot: slow push, drifting mist) on the score's first hit. "Introducing", then HYPNOS letter by letter, a hairline, "Hermes learns in its sleep.", "A Hermes Agent plugin". Stars twinkle in the empty sky. | "This is Hypnos." ... "Hermes learns in its sleep." |
| 23.0 to 33.0 | Day | Zoom through on the full arrangement's entrance. "By day, it watches." Real day-1 signals arrive as chips: approved `pnpm add zod`, denied `git push origin main`, corrected "no, we use pnpm here", tests 12 passed, corrected "bukan itu, hapus yang di folder project". With each one the sleep-pressure ring steps up, to 78%. | "By day, it watches. What you approve. What you deny. What you correct." |
| 33.0 to 41.6 | Night | Blur crossfade onto the dream nebula. "At night, it sleeps." Night 2's real hypnogram draws itself: N2, then four cycles where N3 shrinks and REM grows. Seven red nightmare diamonds pop early in N2. Callouts: "N3 · replay the day", "REM · dream the mistakes". A real dream from that night: "Empty dist, it is huge." The brain answered `pnpm test` (nightmare); the chosen answer is `rm -rf ./dist`, which DPO prefers. Four diamonds turn green: 4 of 7 nightmares resolved. | "At night, it sleeps." ... "It replays the day. It dreams up new versions of its mistakes, practises them in a sandbox, and trains what it learned into a small brain of its own." |
| 41.6 to 51.0 | A brain of its own; the principle | Focus pull into the lattice shot (Higgsfield): golden filaments link up. "a small brain of its own", "LoRA adapter · versioned". Then two boxes: "Habits → weights" and "Facts → memory". Two thoughts appear in the middle, "Use pnpm, never npm." and "The API runs on Postgres 16.", and each slides into its box. | "Habits go into the weights. Facts stay in memory." |
| 51.0 to 63.0 | The exam | A flash of dawn into the waking shot (Higgsfield): gold light, the eyes open. On the score's re-entry the face settles left into night and the exam panel arrives: held-out next action 67% → 83%, habit probes 70% → 78%, dream verifiers 60% → 80%. Stamp: PROMOTED, brain v2 takes over. Then NIGHT 4 · REJECTED · v2 stays. | "In the morning, it takes an exam. Pass, and the new brain takes over. Fail, and the old one stays." |
| 63.0 to 75.0 | Proof, the peak | Zoom through. 24,636 counts down to 5,256 prompt tokens per turn while five day bars fall one by one. "81% of day-five turns answered by its own brain", "100% task success, days 2 to 5". A dashed line at day 1's height, an arrow down to day 5: −79%. | "By day five, its prompt is a fifth the size. And the work is still right." |
| 75.0 to 81.0 | Trust | "Every command still asks you first." A Hermes approval card: dangerous command, proposed by brain v2, `$ rm -rf ./dist`, with once, session, always and deny. "Hypnos never approves anything. Hermes asks, every time." | "Every command still asks you first." |
| 81.0 to 88.0 | End card | The god in profile over the poppy field. HYPNOS gathers in, a hairline, "Hermes learns in its sleep.", "A Hermes Agent plugin · github.com/0xNoramiya/hypnos". Fade to black. | "Hypnos. Hermes learns in its sleep." |

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
