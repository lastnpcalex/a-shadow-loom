"""Allowlisted Admin settings, with labels shared by API and browser."""
import math

# key, label, type, minimum, maximum, explanation
FIELDS = [
    ('default_permission_profile', 'Default trust level', 'profile', None, None, 'Applies immediately to new conversations, including ExoHome. Existing conversations and forks retain their saved trust level.'),
    ('llama_host', 'Local server URL', 'text', None, None, 'OpenAI-compatible endpoint used by local modes.'),
    ('llama_model', 'Default local model', 'text', None, None, 'Saved model filename. Apply through the Llama controls in Overview.'),
    ('llama_server_exe', 'Local server executable', 'text', None, None, 'Path to llama-server used when launching the local server.'),
    ('temperature', 'Temperature', 'number', 0, 2, 'Local sampling randomness. Higher values give more varied responses.'),
    ('top_p', 'Top-p', 'number', 0, 1, 'Local sampling cutoff. Lower values narrow word choices.'),
    ('max_tokens', 'Default output limit', 'integer', 64, 131072, 'Maximum tokens for direct local generation and helper passes.'),
    ('repeat_penalty', 'Repetition penalty', 'number', 1, 2, 'Local pressure against repeated wording. Higher values can make wording brittle.'),
    ('max_context_tokens', 'Local history budget', 'integer', 4096, 262144, 'History budget used by local context preparation.'),
    ('verbatim_window', 'Recent messages kept exact', 'integer', 2, 50, 'Messages retained word for word when local history is summarized.'),
    ('enable_umans_models', 'Show legacy Umans models', 'boolean', None, None, 'Compatibility option for existing Umans conversations.'),
]
TUNING_FIELDS = [
    ('ctx_size', 'Context size', 'integer', 2048, 1048576, 'Runtime token capacity. Takes effect on the next local server launch.'),
    ('ngl', 'GPU layers', 'integer', 0, 999, 'Zero uses CPU layers; 999 requests all layers on GPU.'),
    ('kv_quant', 'KV cache precision', 'text', None, None, 'Use none or a supported quantization such as K8_0.'),
    ('threads', 'CPU threads', 'integer', 1, 64, 'Leave blank for automatic selection.'),
    ('batch', 'Prompt batch size', 'integer', 1, 32768, 'Leave blank for the server default.'),
    ('ubatch', 'Prompt microbatch size', 'integer', 1, 32768, 'Leave blank for the server default.'),
    ('flash_attn', 'Flash attention', 'boolean', None, None, 'Use the faster attention path when supported.'),
    ('mlock', 'Keep model in RAM', 'boolean', None, None, 'Request locked memory to avoid swapping.'),
    ('mmproj', 'Vision projector', 'text', None, None, 'Matching projector filename; leave blank for text-only models.'),
    ('extra_args', 'Additional launch arguments', 'text', None, None, 'Raw llama-server arguments appended on launch.'),
]

def schema(fields):
    return [dict(zip(('key', 'label', 'type', 'min', 'max', 'description'), field)) for field in fields]

def validate(values, fields):
    if not isinstance(values, dict):
        raise ValueError('Settings must be an object')
    specs = {field[0]: field for field in fields}
    if set(values) - specs.keys():
        raise ValueError('Unknown setting')
    result = {}
    for key, value in values.items():
        _, label, kind, low, high, _ = specs[key]
        if value is None and key in {'threads', 'batch', 'ubatch'}:
            result[key] = None
        elif kind == 'profile' and value in ('strict', 'balanced', 'trusted', 'trust_review'):
            result[key] = value
        elif kind == 'boolean' and isinstance(value, bool):
            result[key] = value
        elif kind == 'text' and isinstance(value, str) and len(value) <= 4096:
            result[key] = value.strip()
        elif kind in {'number', 'integer'} and not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and low <= value <= high and (kind != 'integer' or value == int(value)):
            result[key] = int(value) if kind == 'integer' else value
        else:
            raise ValueError(f'{label}: invalid value')
    return result
