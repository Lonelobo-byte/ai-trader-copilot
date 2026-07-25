import json
import logging

logger = logging.getLogger(__name__)

def repair_json_string(s: str) -> str:
    """Repair common JSON formatting issues from LLM outputs.
    
    Fixes:
      1. Strips out-of-bounds characters or markdown blocks by matching '{' and '}'.
      2. Escapes nested, unescaped double quotes inside string values.
      3. Replaces raw newlines, tabs, and carriage returns inside string values with safe escapes.
      4. Strips trailing commas before closing braces/brackets.
    """
    # 1. Strip out markdown wraps
    start_idx = s.find("{")
    end_idx = s.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        s = s[start_idx:end_idx + 1]
    
    chars = list(s)
    n = len(chars)
    repaired = []
    
    in_string = False
    i = 0
    while i < n:
        c = chars[i]
        
        if in_string:
            if c == '\\':
                # Preserve escape sequences
                repaired.append(c)
                if i + 1 < n:
                    repaired.append(chars[i+1])
                    i += 2
                else:
                    i += 1
                continue
            elif c == '"':
                # Check if it's a closing quote.
                # Look ahead for next non-whitespace char.
                next_non_ws = None
                j = i + 1
                while j < n:
                    if chars[j] not in (' ', '\t', '\n', '\r'):
                        next_non_ws = chars[j]
                        break
                    j += 1
                
                # Heuristic: closing quote if followed by JSON structural separator or end of file
                if next_non_ws is None or next_non_ws in (':', ',', '}', ']'):
                    in_string = False
                    repaired.append(c)
                else:
                    # Nested unescaped quote -> escape it
                    repaired.append('\\')
                    repaired.append('"')
            elif c == '\n':
                repaired.append('\\')
                repaired.append('n')
            elif c == '\r':
                repaired.append('\\')
                repaired.append('r')
            elif c == '\t':
                repaired.append('\\')
                repaired.append('t')
            else:
                repaired.append(c)
            i += 1
        else:
            if c == '"':
                in_string = True
                repaired.append(c)
            elif c == ',':
                # Check for trailing comma
                next_non_ws = None
                j = i + 1
                while j < n:
                    if chars[j] not in (' ', '\t', '\n', '\r'):
                        next_non_ws = chars[j]
                        break
                    j += 1
                if next_non_ws in ('}', ']'):
                    # Skip trailing comma
                    pass
                else:
                    repaired.append(c)
            else:
                repaired.append(c)
            i += 1
            
    return "".join(repaired)


def loads_repaired(s: str) -> dict:
    """Robust json.loads wrapper that attempts to repair syntax errors."""
    clean = s.strip()
    try:
        # Fast path
        start_idx = clean.find("{")
        end_idx = clean.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            clean = clean[start_idx:end_idx + 1]
        return json.loads(clean)
    except json.JSONDecodeError as exc:
        logger.warning(f"Initial json.loads failed: {exc}. Attempting repair...")
        repaired = repair_json_string(s)
        try:
            return json.loads(repaired)
        except Exception as inner_exc:
            logger.error(f"Failed to parse JSON even after repair. Repaired string: {repaired}. Error: {inner_exc}")
            raise exc
