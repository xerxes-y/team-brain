"""Optional dense embedders that switch memento's pgvector ANN path **on**.

memento's ``MemoryStorePG`` stores a real ``vector`` column and ranks it with
``<=>`` (cosine ANN) *only* when it is handed a **dense embedder**. The contract
is tiny — the store calls exactly two attributes (see
``memento_memory_pg.MemoryStorePG._dense_literal`` / ``_init_db``):

  * ``dim``                     — the fixed embedding dimension (``int``)
  * ``embed(text) -> list[float]`` — a length-``dim`` vector

Without one, memento falls back to its deterministic term-frequency cosine, i.e.
lexical search. This module builds an embedder from environment variables so the
rest of team-brain stays env-driven (just like ``MEMENTO_DB_URL`` and the
connectors).

team-brain itself ships **no** ML dependency: the default OpenAI backend talks
to the REST API over stdlib ``urllib`` (the connectors do the same). A local
``sentence-transformers`` backend is used only if that package is installed.

Configuration (all optional; unset ⇒ no dense embedder ⇒ lexical search)::

    TEAMBRAIN_EMBED          openai | local | none    (default: none)
    TEAMBRAIN_EMBED_PROFILE  demo | cpu | gpu-small | gpu-large | server
                             a resource-tier preset for backend+model+dim;
                             individual TEAMBRAIN_EMBED* vars still override it
    TEAMBRAIN_EMBED_MODEL    model name (preset/provider default otherwise)
    TEAMBRAIN_EMBED_DIM      output dimension (Matryoshka-truncated if supported)
    TEAMBRAIN_EMBED_DEVICE   cpu | cuda | mps   (local backend; auto-detected)
    TEAMBRAIN_EMBED_BASE_URL embed server URL; falls back to OPENAI_BASE_URL —
                             set it when embeddings and chat use DIFFERENT servers
    TEAMBRAIN_EMBED_API_KEY  embed server key; falls back to OPENAI_API_KEY.
                             A key is only required for api.openai.com — local
                             servers (Ollama / vLLM / TEI) run keyless
    OPENAI_BASE_URL          shared default for any compatible server

The point of the **profiles** is the demo → production path: start on `demo`
(CPU, no key), then as you get a GPU flip ``TEAMBRAIN_EMBED_PROFILE`` to
``gpu-small`` / ``gpu-large`` (or ``server`` for a shared GPU box) — no code
change. Switching model/dimension changes the vector space, so re-embed existing
rows afterwards with ``python3 -m teambrain.reindex``.

The embedding dimension is baked into the Postgres column the first time the
store is initialised, so do **not** change model/dimension against an existing
pgvector table without re-indexing it.
"""
from __future__ import annotations

import json
import os
import urllib.request

_OFF = {"", "none", "off", "0", "false", "no"}

# Resource-tier presets — the "adaptor". Each maps to (backend, model, dim).
# Pick one via TEAMBRAIN_EMBED_PROFILE; any explicit TEAMBRAIN_EMBED* var wins.
PROFILES = {
    # name        backend    model                          dim    ~footprint
    "demo":      ("local",  "BAAI/bge-small-en-v1.5",       None),  # CPU, ~0.3GB, no key
    "cpu":       ("local",  "BAAI/bge-base-en-v1.5",        None),  # CPU, better quality
    "gpu-small": ("local",  "Qwen/Qwen3-Embedding-4B",      1024),  # ~4-9GB VRAM
    "gpu-large": ("local",  "Qwen/Qwen3-Embedding-8B",      1024),  # ~16GB VRAM (fp16)
    "server":    ("openai", None,                           None),  # remote GPU via OPENAI_BASE_URL
}


def _auto_device() -> str:
    """Pick the best local device available (cuda > mps > cpu)."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class OpenAIEmbedder:
    """Dense embedder backed by the OpenAI embeddings REST API (stdlib only)."""

    # Native output dimensions for the common models; ``text-embedding-3-*`` can
    # also be truncated server-side via the ``dimensions`` request field.
    _DIMS = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, model=None, api_key=None, base_url=None, dim=None):
        self.model = model or "text-embedding-3-small"
        self._key = (api_key or os.environ.get("TEAMBRAIN_EMBED_API_KEY")
                     or os.environ.get("OPENAI_API_KEY", ""))
        self._base = (base_url or os.environ.get("TEAMBRAIN_EMBED_BASE_URL")
                      or os.environ.get("OPENAI_BASE_URL")
                      or "https://api.openai.com/v1").rstrip("/")
        if not self._key and "api.openai.com" in self._base:
            raise RuntimeError(
                "OPENAI_API_KEY (or TEAMBRAIN_EMBED_API_KEY) is required for "
                "api.openai.com; local embed servers need a base URL instead")
        self.dim = int(dim) if dim else self._DIMS.get(self.model, 1536)

    def embed(self, text):
        body = {"model": self.model, "input": text or " "}
        # Only v3 models honour a custom output dimension.
        if self.dim and self.model.startswith("text-embedding-3"):
            body["dimensions"] = self.dim
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(
            self._base + "/embeddings",
            data=json.dumps(body).encode(), headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = json.loads(r.read().decode())
        return payload["data"][0]["embedding"]


class LocalEmbedder:
    """Dense embedder backed by a local ``sentence-transformers`` model.

    Requires ``pip install sentence-transformers`` (heavy; imported lazily so the
    rest of team-brain stays dependency-free)."""

    def __init__(self, model=None, dim=None, device=None):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "TEAMBRAIN_EMBED=local needs 'sentence-transformers' "
                "(pip install sentence-transformers)") from exc
        self.model = model or "BAAI/bge-small-en-v1.5"
        self.device = device or os.environ.get("TEAMBRAIN_EMBED_DEVICE") or _auto_device()
        kw = {"device": self.device}
        # Matryoshka (MRL) truncation: ask the model for a smaller vector so the
        # pgvector column stays cheap (e.g. Qwen3 4096 -> 1024). Ignored by models
        # that don't support it beyond a plain slice.
        if dim:
            kw["truncate_dim"] = int(dim)
        self._m = SentenceTransformer(self.model, **kw)
        # method was renamed across sentence-transformers versions
        _get_dim = (getattr(self._m, "get_embedding_dimension", None)
                    or self._m.get_sentence_embedding_dimension)
        self.dim = int(dim) if dim else _get_dim()

    def embed(self, text):
        v = self._m.encode(text or " ", normalize_embeddings=True)
        return [float(x) for x in v]


def resolve_config():
    """Resolve (backend, model, dim) from the profile + explicit env overrides.

    Returns ``(None, None, None)`` when no embedder is configured (lexical)."""
    profile = (os.environ.get("TEAMBRAIN_EMBED_PROFILE") or "").strip().lower()
    p_backend = p_model = p_dim = None
    if profile and profile not in _OFF:
        if profile not in PROFILES:
            raise ValueError(
                f"unknown TEAMBRAIN_EMBED_PROFILE={profile!r}; "
                f"choose one of {sorted(PROFILES)}")
        p_backend, p_model, p_dim = PROFILES[profile]

    backend = (os.environ.get("TEAMBRAIN_EMBED") or p_backend or "none").strip().lower()
    if backend in _OFF:
        return None, None, None
    model = os.environ.get("TEAMBRAIN_EMBED_MODEL") or p_model
    dim = os.environ.get("TEAMBRAIN_EMBED_DIM") or p_dim
    return backend, model, (int(dim) if dim else None)


def make_dense_embedder():
    """Build the dense embedder from the resolved profile/env config.

    Returns ``None`` when nothing is configured (the lexical default). Raises if
    a backend is requested but mis-configured — failing loudly beats silently
    degrading semantic search the operator explicitly asked for."""
    backend, model, dim = resolve_config()
    if backend is None:
        return None
    if backend == "openai":
        return OpenAIEmbedder(model=model, dim=dim)
    if backend == "local":
        return LocalEmbedder(model=model, dim=dim)
    raise ValueError(
        f"unknown TEAMBRAIN_EMBED={backend!r} (use 'openai', 'local', or 'none')")
