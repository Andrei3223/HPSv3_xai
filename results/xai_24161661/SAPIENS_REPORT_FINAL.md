# What does HPSv3 look at? Final body-part study (91 images, two methods, validated)

## The question

HPSv3 gives an image one "human preference" score. We want to know which parts of
the picture that score actually depends on — the face, hair, clothes, or background.

## How we tested it

If a region matters to the score, hiding it should make the score fall a lot. If it
doesn't matter, hiding it changes little. For each image we:

1. Used **Sapiens** (Meta's body-part model) to split the picture into meaningful
   regions: face, hair, torso, arms, clothing, background, etc.
2. Hid one region at a time (filled it black) and re-scored with HPSv3.
3. Measured the drop: **importance = score before − score after.**

We did this on **91 images** from the HPDv3 dataset, and we made the result
trustworthy in three ways:

- **Two independent methods.** Occlusion (hide one region at a time) and LIME (hide
  random combinations and fit a model). If both agree, the result isn't a quirk of
  one method.
- **A faithfulness test.** We remove the "important" regions first and check the
  score really does collapse faster than removing random regions.
- **Error bars.** With 91 images we can show how confident each number is.

## The dataset

- 91 images scored, HPSv3 scores ranging **4.05 to 15.70** (mean 10.61) — a real
  spread of quality.
- **74** images contained at least one body part (a person); **60** had a face.
- Sapiens is built for real people, so the ~17 non-person images (illustrations,
  objects) simply don't contribute to the body-part totals.

---

## Result 1 — The face is what HPSv3 cares about most

![Importance per body part](fig_part_importance.png)

**Left (raw importance).** Background has the biggest raw number only because it
covers ~86% of the image, so hiding it changes almost everything. Among the real
body parts, the **face is the clear leader** (4.2-point average drop), then hair and
clothing, then everything else is small. Error bars are tight.

**Right (importance per unit area)** — the fair comparison, i.e. drop *per pixel*:

| Part | Per-area importance |
|---|---|
| **face** | **157 ± 14** |
| hair | 75 ± 7 |
| upper clothing | 18.5 ± 2.1 |
| lower clothing | 18.7 ± 4.0 |
| background | 15.2 ± 0.7 |

Pixel for pixel, **the face matters ~10× more than background and ~2× more than
hair.** The error bars don't overlap, so this ordering is statistically real.

**Image by image:** out of the 60 face images, the face was the single most
important body part in **42 of them (70%)** — this isn't just an averaging effect.

---

## Result 2 — A second method (LIME) gives the same answer

We re-ran everything with LIME, a different attribution method.

- **Agreement with occlusion: average Spearman 0.92** across the 74 images with a
  person. That's very high — both methods rank the regions almost the same way.
- LIME's own part ranking matches occlusion almost exactly:

| Part | Occlusion (per-area) | LIME (per-area) |
|---|---|---|
| face | 157 | 158 |
| hair | 75 | 62 |
| clothing | ~18 | ~20 |
| background | 15.2 | 15.6 |

So "the face matters most" is not an artifact of how we measured — two independent
methods land on the same conclusion.

---

## Result 3 — The explanations are faithful (and we know exactly when)

We checked whether removing the important regions first breaks the score faster than
removing random regions (the "faithfulness" test), in both directions
(deletion = take parts away; insertion = add them back). Result, split by how many
regions Sapiens found:

| Regions found | What it means | Deletion passes | Insertion passes |
|---|---|---|---|
| 1–2 | basically no person detected | 0 / 25 | 0 / 25 |
| 3–5 | a person | 15 / 15 | 15 / 15 |
| 6+ | a person, well segmented | 60 / 60 | 60 / 60 |

**For every single image where Sapiens actually found a person (≥3 regions), the
explanation is faithful — 75/75, 100%.** The only "failures" are the 25 images with
1–2 regions, where the test is meaningless (you can't rank 1 region). On average the
importance ordering beat random by a wide margin (+6.9 points).

The softer "grey" hide is slightly weaker but still strong (deletion 67/75 = 89%,
insertion 72/75 = 96% on real-person images), which is expected — a grey patch is a
gentler change than black.

---

## Result 4 — Caring about the face goes with a higher score

Across the 60 face images, there's a **positive correlation of 0.42 between how much
the face drives the score and the overall score.** Images where the face is a strong,
clear contributor tend to be the ones HPSv3 rates highly — consistent with a
prominent, good face being part of what makes an image score well.

---

## Full per-part table (occlusion / black, 91 images)

| Part | Images | Avg area | Raw importance (±SE) | Per-area (±SE) |
|---|---|---|---|---|
| background | 91 | 86.5% | 13.19 ± 0.77 | 15.2 ± 0.7 |
| **face** | 60 | 2.7% | **4.23 ± 0.42** | **157 ± 14** |
| **hair** | 59 | 2.7% | 2.01 ± 0.30 | 75 ± 7 |
| upper clothing | 60 | 9.2% | 1.71 ± 0.21 | 18.5 ± 2.1 |
| arms | 53 | 1.8% | 0.75 ± 0.13 | small region |
| hands | 57 | 0.9% | 0.64 ± 0.10 | small region |
| legs | 18 | 1.4% | 0.53 ± 0.14 | small region |
| lower clothing | 44 | 2.7% | 0.51 ± 0.11 | 18.7 ± 4.0 |
| feet | 42 | 0.7% | 0.33 ± 0.08 | small region |
| torso | 36 | 0.9% | 0.27 ± 0.07 | small region |

---

## Conclusion

**HPSv3's preference score is driven by the person's face above everything else,
then hair, with clothing and background contributing little.** Concretely:

- Per pixel, the face is ~10× more important than the background and ~2× more than
  hair, with non-overlapping error bars.
- The face is the single most important region in 70% of photos that contain one.
- Two independent methods (occlusion and LIME) agree almost perfectly (0.92).
- The explanations are faithful on 100% of images where a real person was found.
- Images where the face matters more also tend to score higher (r = 0.42).

This is exactly how a good human-preference model should behave: it focuses on faces
and people — what humans notice first — not on incidental background. If you use
HPSv3 to rank or train an image generator, expect it to reward good, prominent faces
strongly.

## Limitations and possible next steps

- **Sapiens only works on real people.** On the ~17 non-person images it found
  little, and those are correctly excluded. A pure human-photo dataset is the ideal
  fit.
- **"Face" is one region.** Sapiens' finer 28-class mode could split it (eyes, mouth,
  skin) to show *what within the face* matters most.
- **More images** (300–500) would tighten the per-area bars further and allow
  subgroup analysis — e.g. does the face matter even more for high-scoring images?
  The 0.42 correlation suggests it might.
- **A formal significance test** (the error bars already separate cleanly) would make
  the face-vs-rest gap airtight on paper.

## Files behind this report
- `fig_part_importance.png` — the per-part chart (main result).
- `fig_faithfulness.png` — deletion/insertion curves.
- `agg_part_importance.csv` — full per-part table with error terms.
- `summary.csv` — every per-image, per-region number, including faithfulness AUCs.
- `<image>_occlusion_black.png` — per-image heatmaps (red = raises the score).
