"""
Gemini LLM Client — the "brain" of Alpha Scout.

Mirrors BDC Footnotes pattern: single client, Langfuse tracing, retry logic.
Adds a grounding guard that forces Gemini to cite sources or return "N/A".

Required env var: GEMINI_API_KEY (https://aistudio.google.com/apikey)
"""

import os
import json
import time
import logging
import concurrent.futures
from typing import Dict, Optional

from google import genai
from tracing import create_generation, create_trace, get_langfuse

logger = logging.getLogger(__name__)

# --- Config ---
MODEL_FLASH = "gemini-2.5-flash"      # Fast model for search, extraction, summary
MODEL_PRO = "gemini-2.5-pro"          # Powerful model for scoring (Step 3)
MODEL_ID = MODEL_FLASH                 # Default model
CALL_DELAY_SECONDS = 1.5
MAX_RETRIES = 3
TIMEOUT_SECONDS = 120

# Prepended to EVERY prompt — forces factual, cited output
GROUNDING_INSTRUCTION = """You are a research analyst. You MUST follow these rules:
1. ONLY use information from the provided source data. Do NOT use your training data.
2. For EVERY claim, cite the source URL it came from.
3. If information is not available in the sources, return "N/A" or "Not Found".
4. NEVER guess or invent founder names, LinkedIn profiles, financial metrics, or descriptions.
5. Always respond in valid JSON format when asked for structured output.
6. If uncertain, say "Unverified" and explain why.
CRITICAL: It is better to return "Not Found" than to hallucinate a wrong answer."""


def _get_client() -> genai.Client:
    """Create a Gemini client from env var. Raises ValueError if key missing."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY not set. Get yours at https://aistudio.google.com/apikey"
        )
    return genai.Client(api_key=api_key)


def call_gemini(
    prompt: str,
    trace=None,
    span_name: str = "gemini_call",
    metadata: dict = None,
    temperature: float = 0.0,
    use_pro_model: bool = False,
) -> str:
    """
    Single entry point for ALL Gemini calls. Every call automatically gets:
    - Grounding instruction prepended (forces citations, blocks hallucinations)
    - Langfuse generation tracing (logs prompt + response to dashboard)
    - Retry with exponential backoff (handles transient API failures)
    - Thread-based timeout (cancels if Gemini hangs)
    
    Args:
        use_pro_model: If True, use gemini-2.5-pro for complex scoring tasks.
                       Default False uses gemini-2.5-flash for speed.
    """
    time.sleep(CALL_DELAY_SECONDS)
    
    # Select model based on task complexity
    model_to_use = MODEL_PRO if use_pro_model else MODEL_FLASH

    # Prepend grounding rules to every prompt
    full_prompt = f"{GROUNDING_INSTRUCTION}\n\n---\n\n{prompt}"

    # Log to Langfuse — auto-create trace if none provided
    generation = None
    lf = get_langfuse()
    if lf is not None:
        # Always log to Langfuse, even without explicit trace
        generation = create_generation(
            name=span_name, model=model_to_use,
            input_data=full_prompt, metadata=metadata or {},
        )

    def make_api_call():
        """Runs in a thread so we can enforce a timeout."""
        client = _get_client()
        response = client.models.generate_content(
            model=model_to_use, contents=full_prompt,
            config={"temperature": temperature, "max_output_tokens": 16384},
        )
        return response.text.strip()

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(make_api_call)
                output_text = future.result(timeout=TIMEOUT_SECONDS)

            # Log response to Langfuse
            if generation is not None:
                try:
                    generation.update(output=output_text)
                    generation.end()
                except Exception:
                    pass
            return output_text

        except concurrent.futures.TimeoutError:
            last_error = TimeoutError(f"API call timed out after {TIMEOUT_SECONDS}s")
            logger.warning(f"Timeout (attempt {attempt+1}/{MAX_RETRIES}), retrying...")
            time.sleep(2 ** attempt)
        except Exception as e:
            last_error = e
            if any(kw in str(e).lower() for kw in ["timed out", "timeout", "reset"]):
                logger.warning(f"Transient error (attempt {attempt+1}): {e}, retrying...")
                time.sleep(2 ** attempt)
            else:
                break  # Non-retryable error

    # All retries failed — log error and raise
    if generation is not None:
        try:
            generation.update(output=f"ERROR: {last_error}", level="ERROR")
            generation.end()
        except Exception:
            pass
    raise last_error


def _repair_truncated_json(text: str) -> Optional[str]:
    """
    Attempt to repair truncated JSON by finding complete objects.
    
    When Gemini's response is cut off mid-JSON, we try to salvage
    the complete objects that were returned before truncation.
    
    Example:
        Input:  [{"name": "A", ...}, {"name": "B", "field": "trun
        Output: [{"name": "A", ...}]
    
    Returns repaired JSON string or None if repair not possible.
    """
    text = text.strip()
    
    # Handle arrays (most common for company extraction)
    if text.startswith("["):
        # Find all complete objects by tracking brace depth
        objects = []
        current_start = None
        depth = 0
        in_string = False
        escape_next = False
        
        for i, char in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
                
            if char == '{':
                if depth == 0:
                    current_start = i
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0 and current_start is not None:
                    # Found a complete object
                    obj_str = text[current_start:i+1]
                    try:
                        json.loads(obj_str)  # Validate it's valid JSON
                        objects.append(obj_str)
                    except json.JSONDecodeError:
                        pass
                    current_start = None
        
        if objects:
            repaired = "[" + ", ".join(objects) + "]"
            logger.info(f"JSON repair: recovered {len(objects)} complete objects from truncated response")
            return repaired
    
    # Handle single object truncation
    elif text.startswith("{"):
        # Find the last complete nested structure
        depth = 0
        last_valid_end = -1
        in_string = False
        escape_next = False
        
        for i, char in enumerate(text):
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
                
            if char == '{':
                depth += 1
            elif char == '}':
                depth -= 1
                if depth == 0:
                    last_valid_end = i
                    break
        
        if last_valid_end > 0:
            repaired = text[:last_valid_end + 1]
            try:
                json.loads(repaired)
                logger.info("JSON repair: recovered truncated object")
                return repaired
            except json.JSONDecodeError:
                pass
    
    return None


def _extract_objects_regex(text: str) -> Optional[list]:
    """
    Nuclear fallback: extract individual JSON objects using regex.
    
    This handles severely malformed responses by finding anything
    that looks like a complete JSON object.
    """
    import re
    
    # Find potential JSON objects (simple pattern, may have false positives)
    pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
    matches = re.findall(pattern, text)
    
    valid_objects = []
    for match in matches:
        try:
            obj = json.loads(match)
            # Must have a 'name' field to be a company object
            if isinstance(obj, dict) and 'name' in obj:
                valid_objects.append(obj)
        except json.JSONDecodeError:
            continue
    
    if valid_objects:
        logger.info(f"JSON regex extraction: recovered {len(valid_objects)} objects")
        return valid_objects
    
    return None


def parse_json_response(text: str) -> Dict:
    """
    Parse JSON from Gemini with graceful degradation for truncated responses.
    
    Strategies tried in order:
    1. Strip ```json ... ``` fences and parse
    2. Direct json.loads()
    3. Find JSON boundaries and parse
    4. Repair truncated JSON (recover complete objects)
    5. Regex extraction (nuclear fallback)
    """
    cleaned = text.strip()

    # Strategy 1: Strip markdown code fences
    if "```" in cleaned:
        for part in cleaned.split("```"):
            candidate = part.strip()
            if candidate.startswith("json"):
                candidate = candidate[4:].strip()
            if candidate.startswith("{") or candidate.startswith("["):
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # Try repair on the candidate
                    repaired = _repair_truncated_json(candidate)
                    if repaired:
                        try:
                            return json.loads(repaired)
                        except json.JSONDecodeError:
                            pass

    # Strategy 2: Direct parse
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Strategy 3: Find JSON boundaries
    first_brace = cleaned.find("{")
    first_bracket = cleaned.find("[")
    
    # Use whichever comes first
    if first_bracket != -1 and (first_brace == -1 or first_bracket < first_brace):
        # Array - find matching ]
        last_bracket = cleaned.rfind("]")
        if last_bracket > first_bracket:
            try:
                return json.loads(cleaned[first_bracket:last_bracket + 1])
            except json.JSONDecodeError:
                pass
    
    if first_brace != -1:
        last_brace = cleaned.rfind("}")
        if last_brace > first_brace:
            try:
                return json.loads(cleaned[first_brace:last_brace + 1])
            except json.JSONDecodeError:
                pass

    # Strategy 4: Repair truncated JSON
    repaired = _repair_truncated_json(cleaned)
    if repaired:
        try:
            result = json.loads(repaired)
            logger.warning(f"Used JSON repair fallback - partial data recovered")
            return result
        except json.JSONDecodeError:
            pass

    # Strategy 5: Regex extraction (nuclear fallback)
    extracted = _extract_objects_regex(cleaned)
    if extracted:
        logger.warning(f"Used regex extraction fallback - {len(extracted)} objects recovered")
        return extracted

    logger.warning(f"Failed to parse JSON (all strategies exhausted): {text[:200]}")
    return {"error": "Failed to parse response", "raw": text[:500]}
