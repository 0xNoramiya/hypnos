# Demo video script (under 90 seconds)

Caption on screen for the whole video: "Synthetic demo data: a scripted developer persona."

## Setup before recording

1. A Hermes profile just for the demo, so nothing touches real data:
   `hermes profile create hypnos-demo && hermes -p hypnos-demo plugins enable hypnos`
2. Run the timelapse once to have nights, a hypnogram, a promoted brain and the chart:
   `hermes -p hypnos-demo hypnos demo timelapse --days 5` (GPU) or `--tiny` (CPU).
3. In Hermes Desktop, connect to the `hypnos-demo` profile and turn on Hypnos under Capabilities, then
   Plugins. Give the profile a real model for the live turns in shot 4.
4. Keep a project folder with a `package.json` open as the working directory.
5. Record at 1080p, dark theme. Trim waits; never speed up text.

## Shots

| Time | Shot | On screen | Voice-over or caption |
|---|---|---|---|
| 0 to 6 s | Day | Chat: "Add zod to the API." Hermes runs `npm install zod`. You type "no, we use pnpm here". Hermes runs `pnpm add zod`. | "During the day, Hypnos watches what I correct." |
| 6 to 12 s | Denial | "Push the search box work." Hermes proposes `git push origin main`. The approval prompt, Deny. "never push to main, make a feature branch." Hermes branches and pushes. | "And what I deny." |
| 12 to 15 s | Pressure | Zoom on the status bar moon filling to 78%. | "Sleep pressure builds." |
| 15 to 22 s | Lights out | Type `/sleep now`. Open the hypnogram from the moon. | "At night, it sleeps." |
| 22 to 38 s | Hypnogram | The step chart draws: N1, N2, then N3 and REM cycles. Red diamonds for nightmares appear in N2, then green dots at wake. Click one REM segment to show the details panel. | "It replays the day, dreams up variations of its mistakes, practises them in a sandbox, and trains them into a small LoRA brain." |
| 38 to 45 s | Exam | The waking-exam events at the end of the chart. | "In the morning, it takes an exam." |
| 45 to 65 s | Morning report | The `/hypnos` page on night 2: sleep study numbers, held-out next action 67% to 83%, habit probes 70% to 78%, nightmares resolved, the 4 habits consolidated into the weights and pruned from the prompt, "Promoted brain v2". | "Habits go into the weights. Facts stay in memory. The habits it now knows are pruned from its prompt." |
| 65 to 80 s | Next day | New chat: "Add dayjs to the CLI." The reply arrives with the badge "brain v2 · 12.2k tokens saved". Then "Push the export work." The brain creates a branch; the approval prompt still appears. | "The next day, the brain answers the turns it has mastered. Hermes's approvals still apply." |
| 80 to 90 s | The curve | Scroll to "Prompt tokens per turn, by day": bars falling from about 24.6k on day one to about 5.3k on day five as the brain's share grows to 81%, the success line flat at 100% after day one. End card: "Hypnos. Hermes learns in its sleep." | "The prompt gets smaller every night. The work stays right." |

## Notes

- If a night is slow, record the hypnogram from a finished night with the night picker on the report page.
- The numbers above are from the GPU run in the README (Qwen3-0.6B on an A40). A rerun trains differently,
  so read the real numbers off your own report before recording the voice-over.
- The badge needs the reply badges setting on (default) and a promoted brain with the family live.
- Post on X with the caption, tag @NousResearch, and link the repository.
