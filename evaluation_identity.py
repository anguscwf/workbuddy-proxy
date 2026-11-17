"""Strict evaluation evidence. No credentials, network or manuscript persistence."""
import hashlib
import json
import re

PROTOCOL = "ngl-evaluation-identity-v1"
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)


class IdentityError(ValueError):
    def __init__(self, code, diagnostics=None):
        super().__init__("Strict evaluation model identity could not be verified")
        self.code = code
        self.diagnostics = diagnostics or {}


def snapshot(mappings, source_sha256):
    ordered = dict(sorted(mappings.items()))
    encoded = json.dumps(ordered, separators=(",", ":"), ensure_ascii=True).encode()
    return {"protocol": PROTOCOL, "mappings": ordered,
            "mappingDigest": hashlib.sha256(encoded).hexdigest(),
            "proxySourceSha256": source_sha256,
            "maxUpstreamAttempts": 1, "requiresObservedModel": True}


class ObservedIdentity:
    def __init__(self, expected):
        self.expected = expected
        self.observed = None
        self.failure = None
        self.has_model = False
        self.model_type = "missing"
        self.observed_hash = None

    def diagnostics(self):
        return {"hasModel": self.has_model, "modelType": self.model_type,
                "matchesExpected": self.failure is None and self.observed == self.expected,
                "observedModelSha256": self.observed_hash}

    def error(self, code):
        return IdentityError(code, self.diagnostics())

    def feed(self, chunk):
        if "model" not in chunk:
            return
        value = chunk["model"]
        self.has_model = True
        if self.failure is None:
            self.model_type = ("null" if value is None else "boolean" if isinstance(value, bool) else
                               "string" if isinstance(value, str) else "number" if isinstance(value, (int, float)) else
                               "array" if isinstance(value, list) else "object")
            self.observed_hash = hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not isinstance(value, str) or not MODEL.fullmatch(value):
            self.failure = self.failure or "AI_EVALUATION_MODEL_INVALID"
        elif self.observed is not None and self.observed != value:
            self.failure = self.failure or "AI_EVALUATION_MODEL_CONFLICT"
        else:
            self.observed = value

    def verify(self):
        if self.failure:
            raise self.error(self.failure)
        if self.observed is None:
            raise self.error("AI_EVALUATION_MODEL_MISSING")
        if self.observed != self.expected:
            raise self.error("AI_EVALUATION_MODEL_IDENTITY")
        return self.observed
