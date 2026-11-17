"""Strict evaluation evidence. No credentials, network or manuscript persistence."""
import hashlib
import json
import re
from pathlib import Path

PROTOCOL = "ngl-evaluation-identity-v1"
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
IDENTITY_SOURCE_MANIFEST = ("server.py", "evaluation_identity.py", "workbuddy_cdp.py")
IDENTITY_SOURCE_ALGORITHM = "sha256-ordered-raw-files-v1"


def source_fingerprints(base_dir):
    """Bind the reviewed raw source bytes at startup; missing files fail closed."""
    parts = [(Path(base_dir) / name).read_bytes() for name in IDENTITY_SOURCE_MANIFEST]
    return {
        "proxySourceSha256": hashlib.sha256(b"".join(parts)).hexdigest(),
        "identitySourceManifest": list(IDENTITY_SOURCE_MANIFEST),
        "identitySourceAlgorithm": IDENTITY_SOURCE_ALGORITHM,
        "legacyProxySourceSha256": hashlib.sha256(b"".join(parts[:2])).hexdigest(),
        "legacySourceManifest": list(IDENTITY_SOURCE_MANIFEST[:2]),
        "legacySourceAlgorithm": "sha256-two-raw-files-v0-diagnostic-only",
    }


class IdentityError(ValueError):
    def __init__(self, code, diagnostics=None):
        super().__init__("Strict evaluation model identity could not be verified")
        self.code = code
        self.diagnostics = diagnostics or {}


def snapshot(mappings, source_sha256, *, source_metadata=None):
    ordered = dict(sorted(mappings.items()))
    encoded = json.dumps(ordered, separators=(",", ":"), ensure_ascii=True).encode()
    result = {"protocol": PROTOCOL, "mappings": ordered,
            "mappingDigest": hashlib.sha256(encoded).hexdigest(),
            "proxySourceSha256": source_sha256,
            "maxUpstreamAttempts": 1, "requiresObservedModel": True}
    if source_metadata is not None:
        if source_metadata["proxySourceSha256"] != source_sha256:
            raise ValueError("Source metadata does not match the bound fingerprint")
        for key in ("identitySourceManifest", "identitySourceAlgorithm", "legacyProxySourceSha256",
                    "legacySourceManifest", "legacySourceAlgorithm"):
            result[key] = source_metadata[key]
    return result


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
