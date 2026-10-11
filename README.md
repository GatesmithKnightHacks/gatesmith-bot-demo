# Gatesmith private bot demo

Synthetic three-qubit OpenQASM fixture; contains no research or user data.

This repository opts in to Gemini review. Changed diff hunks and bounded surrounding context, measured scores/findings/issues, and selected API knowledge can be sent to the Gemini API. CI contains no Gemini, HF, or GitHub App secrets.

Scoring uses the immutable public engine revision 6b42f722730f19a5c3d9b72e1a19cf8d54a0fedd. The PR adds a CX ladder with a nonadjacent closing edge on line-3. This intentionally changes circuit behavior; the demo shows scoring and review, and makes no equivalence or verified-improvement claim.
