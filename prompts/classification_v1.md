# Kesar & Co. — Email Classification Prompt (v1 Baseline)

Classify the incoming customer email into JSON matching `schemas/llm_output.schema.json`.

Required JSON fields:
- `intent`: one of `order_status`, `product_query`, `return_refund`, `complaint`, `other`
- `secondary_intent`: another intent or `null`
- `order_id`: `KC-####` if mentioned, else `null`
- `sentiment`: `positive`, `neutral`, `negative`, or `angry`
- `language`: `en`, `hi`, or `hinglish`
- `confidence`: number between 0 and 1
- `summary`: brief one-line English summary (max 200 characters)

Return valid JSON only.

