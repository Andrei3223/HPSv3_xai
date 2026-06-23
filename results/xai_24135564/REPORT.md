# HPSv3 Perturbation-Attribution Report

Results directory: `xai_24135564`  
Images: **7**  |  Methods: occlusion, LIME  |  Baselines: gray, mean, blur, black

## Overview

| image | prompt | base_reward | n_superpixels |
| --- | --- | --- | --- |
| 38eb6939 | The image captures a romantic scene featuring a couple in a … | 9.109 | 70 |
| example1 | cute chibi anime cartoon fox, smiling wagging tail with a sm… | 10.823 | 82 |
| 0f019494 | Painting of a boy sitting with a puppy. | 15.51 | 77 |
| e7361205 | Asian woman, gray strapless top, hand on chin, white backgro… | 11.182 | 78 |
| 047eb1a3 | Couple enjoys wine on balcony under a full moon. | 9.491 | 66 |
| example2 | cute chibi anime cartoon fox, smiling wagging tail with a sm… | 7.163 | 70 |
| d534420e | The image presents a serene and captivating scene set within… | 9.579 | 62 |

## 1. What the model rewards (per image)

### 38eb6939 — base reward μ = 9.109

Prompt: *The image captures a romantic scene featuring a couple in a passionate embrace. The woman, with auburn hair and fair skin, is reclining on a lavish cream-colored chaise lounge with intricate gilded details. She is wearing a flowing green gown that cascades around her, with shades of blue shimmering *

![montage](montage_38eb6939.png)

- Strongest positive region (occlusion/black): **+2.79**
- Fraction of regions that *hurt* the score: **28.6%** (min -0.24)

### example1 — base reward μ = 10.823

Prompt: *cute chibi anime cartoon fox, smiling wagging tail with a small cartoon heart above sticker*

![montage](montage_example1.png)

- Strongest positive region (occlusion/black): **+4.27**
- Fraction of regions that *hurt* the score: **7.3%** (min -0.14)

### 0f019494 — base reward μ = 15.510

Prompt: *Painting of a boy sitting with a puppy.*

![montage](montage_0f019494.png)

- Strongest positive region (occlusion/black): **+8.27**
- Fraction of regions that *hurt* the score: **14.3%** (min -0.09)

### e7361205 — base reward μ = 11.182

Prompt: *Asian woman, gray strapless top, hand on chin, white background.*

![montage](montage_e7361205.png)

- Strongest positive region (occlusion/black): **+5.04**
- Fraction of regions that *hurt* the score: **0.0%** (min +0.11)

### 047eb1a3 — base reward μ = 9.491

Prompt: *Couple enjoys wine on balcony under a full moon.*

![montage](montage_047eb1a3.png)

- Strongest positive region (occlusion/black): **+5.75**
- Fraction of regions that *hurt* the score: **7.6%** (min -0.07)

### example2 — base reward μ = 7.163

Prompt: *cute chibi anime cartoon fox, smiling wagging tail with a small cartoon heart above sticker*

![montage](montage_example2.png)

- Strongest positive region (occlusion/black): **+3.00**
- Fraction of regions that *hurt* the score: **4.3%** (min -0.30)

### d534420e — base reward μ = 9.579

Prompt: *The image presents a serene and captivating scene set within a dense forest. A woman, adorned in a striking red gown, sits gracefully amidst the woodland landscape. Her dark hair complements the richness of her dress, and her gaze is directed slightly upwards, as if contemplating the surrounding nat*

![montage](montage_d534420e.png)

- Strongest positive region (occlusion/black): **+6.67**
- Fraction of regions that *hurt* the score: **29.0%** (min -0.14)

## 2. Occlusion vs LIME agreement (cross-method validation)

![method agreement](fig_method_agreement.png)

| image | mode | spearman_occ_vs_lime |
| --- | --- | --- |
| 38eb6939 | gray | 0.28 |
| 38eb6939 | mean | 0.28 |
| 38eb6939 | blur | 0.429 |
| 38eb6939 | black | 0.381 |
| example1 | gray | 0.136 |
| example1 | mean | 0.29 |
| example1 | blur | 0.363 |
| example1 | black | 0.42 |
| 0f019494 | gray | 0.39 |
| 0f019494 | mean | 0.432 |
| 0f019494 | blur | 0.449 |
| 0f019494 | black | 0.292 |
| e7361205 | gray | 0.482 |
| e7361205 | mean | 0.589 |
| e7361205 | blur | 0.539 |
| e7361205 | black | 0.448 |
| 047eb1a3 | gray | 0.398 |
| 047eb1a3 | mean | 0.439 |
| 047eb1a3 | blur | 0.576 |
| 047eb1a3 | black | 0.435 |
| example2 | gray | 0.508 |
| example2 | mean | 0.375 |
| example2 | blur | 0.412 |
| example2 | black | 0.091 |
| d534420e | gray | 0.204 |
| d534420e | mean | 0.616 |
| d534420e | blur | 0.315 |
| d534420e | black | 0.459 |

Mean Spearman = **0.39** (moderate). Positive across the board → the two methods localize the same regions.

## 3. Baseline (color-perturbation) sensitivity

![baseline magnitude](fig_baseline_magnitude.png)

![baseline agreement](fig_baseline_agreement.png)

| image | mode | spearman_vs_black |
| --- | --- | --- |
| 38eb6939 | gray | 0.496 |
| 38eb6939 | mean | 0.566 |
| 38eb6939 | blur | 0.458 |
| 38eb6939 | black | 1.0 |
| example1 | gray | 0.298 |
| example1 | mean | 0.238 |
| example1 | blur | 0.264 |
| example1 | black | 1.0 |
| 0f019494 | gray | 0.573 |
| 0f019494 | mean | 0.531 |
| 0f019494 | blur | 0.413 |
| 0f019494 | black | 1.0 |
| e7361205 | gray | 0.608 |
| e7361205 | mean | 0.585 |
| e7361205 | blur | 0.214 |
| e7361205 | black | 1.0 |
| 047eb1a3 | gray | 0.423 |
| 047eb1a3 | mean | 0.389 |
| 047eb1a3 | blur | 0.391 |
| 047eb1a3 | black | 1.0 |
| example2 | gray | 0.055 |
| example2 | mean | 0.378 |
| example2 | blur | 0.184 |
| example2 | black | 1.0 |
| d534420e | gray | 0.576 |
| d534420e | mean | 0.668 |
| d534420e | blur | 0.523 |
| d534420e | black | 1.0 |

Mean agreement of soft baselines vs black = **0.42** (moderate). Black gives the largest magnitudes; the choice of baseline shifts which regions look most important (the 'missingness' effect) → report multiple baselines.

## 4. Faithfulness (deletion / insertion)

![faithfulness](fig_faithfulness.png)

| image | method | mode | deletion_auc | deletion_random | deletion_ok | insertion_auc | insertion_random | insertion_ok |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 047eb1a3 | occlusion | black | -6.5483 | -2.9322 | ✓ | 3.5804 | -0.2743 | ✓ |
| 047eb1a3 | occlusion | gray | 5.5548 | 5.9511 | ✓ | 6.961 | 6.4703 | ✓ |
| 0f019494 | occlusion | black | -6.3347 | -1.1148 | ✓ | 8.5621 | 4.3742 | ✓ |
| 0f019494 | occlusion | gray | 10.7398 | 11.8679 | ✓ | 12.9995 | 12.0593 | ✓ |
| 38eb6939 | occlusion | black | -1.0414 | 2.0046 | ✓ | 6.3626 | 4.1433 | ✓ |
| 38eb6939 | occlusion | gray | 6.3069 | 5.9804 | ✗ | 7.5708 | 6.8767 | ✓ |
| d534420e | occlusion | black | -7.0014 | -2.9634 | ✓ | 7.804 | 5.737 | ✓ |
| d534420e | occlusion | gray | 7.2042 | 7.1575 | ✗ | 8.6759 | 8.2004 | ✓ |
| e7361205 | occlusion | black | -6.6206 | -1.8899 | ✓ | 3.9433 | -2.5596 | ✓ |
| e7361205 | occlusion | gray | 6.8723 | 6.3433 | ✗ | 8.5909 | 5.8618 | ✓ |
| example1 | occlusion | black | -3.006 | 0.6713 | ✓ | 5.2058 | 0.7635 | ✓ |
| example1 | occlusion | gray | 8.3696 | 8.9741 | ✓ | 9.6197 | 8.8503 | ✓ |
| example2 | occlusion | black | -3.2176 | 0.211 | ✓ | 3.0077 | -0.4138 | ✓ |
| example2 | occlusion | gray | 4.9462 | 6.0843 | ✓ | 7.0628 | 5.6933 | ✓ |

Insertion passes **14/14**, deletion passes **11/14**. Insertion is the more reliable signal; with soft baselines (gray) the deletion AUC can be inflated by the curve recovering toward the neutral-baseline score — compare the black baseline for a cleaner deletion test.

## 6. Limitations

- Sample size: **7 images** — illustrative, not statistically general.

- All images are high-scoring; no low-quality contrast case.

- LIME has sampling noise; check stability across seeds before strong claims.
