# Troubleshooting

Load this file when you hit:

- SSL certificate errors during `pip install` or model download
- `huggingface.co` unreachable, blocked, or returning 403
- Enterprise / corporate network, proxy, or airgapped machine
- `sqlite-vec` fails to load at runtime
- `sqlite3.enable_load_extension` "not authorized"
- Indexer hangs on first run

## SSL / cert interception (corporate networks)

Symptoms:

```
pip: SSL: CERTIFICATE_VERIFY_FAILED
fastembed: SSLError: HTTPSConnectionPool(host='huggingface.co', ...)
```

Most corporate networks do TLS interception with their own CA. Python
and pip won't trust that CA until you tell them about it.

### Step 1 — get the corp CA cert

Ask IT, or export from your browser's trusted root store. Save as
`corp-ca.pem`. If there's a chain, concatenate:

```bash
cat root-ca.pem intermediate-ca.pem > corp-ca.pem
```

### Step 2 — fix pip

```bash
# persistent
pip config set global.cert /path/to/corp-ca.pem

# one-off
pip install --cert /path/to/corp-ca.pem -r requirements.txt
```

If you also need an internal mirror:

```bash
pip config set global.index-url https://nexus.corp/pypi/simple
```

### Step 3 — fix Python's HTTPS stack (for fastembed model download)

`fastembed` uses `huggingface_hub`, which uses `requests`. Set these
environment variables in `~/.bashrc` / `~/.zshrc`:

```bash
export SSL_CERT_FILE=/path/to/corp-ca.pem
export REQUESTS_CA_BUNDLE=/path/to/corp-ca.pem
export CURL_CA_BUNDLE=/path/to/corp-ca.pem
```

Verify:

```bash
python -c "import urllib.request; print(urllib.request.urlopen('https://huggingface.co').status)"
# expect: 200
```

Then run the indexer normally.

## Offline / airgapped — pre-stage the model

If you can't configure certs or HF is blocked entirely, download the
model on a connected machine and ship the cache:

```bash
# on a machine with internet
pip install fastembed
python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5').embed(['warmup'])"
# cache now lives under ~/.cache/huggingface/

# tar it up
tar czf fastembed-cache.tgz -C ~ .cache/huggingface

# on the target machine
tar xzf fastembed-cache.tgz -C ~
export FASTEMBED_CACHE_PATH=~/.cache/huggingface   # usually auto-picked
```

Now `python scripts/index.py --full` works offline.

### Pointing fastembed at a local cache explicitly

If you need fastembed to refuse network calls entirely, pass
`cache_dir` + `local_files_only=True` when constructing
`TextEmbedding`. This requires a small edit to `Embedder.ensure()` in
`scripts/index.py`:

```python
from fastembed import TextEmbedding
self.model = TextEmbedding(
    model_name=self.model_name,
    cache_dir="/mnt/shared/models",
    local_files_only=True,
)
```

## Fallback — skip embeddings entirely

All of the above is only needed for semantic `--search`. The indexer
has a flag to turn it off:

```bash
python scripts/index.py --root . --db .kg/code_kg.sqlite --full --no-embeddings
```

You still get:

- FTS5 keyword search
- Full call graph (callers, callees, neighbors)
- All AST queries (functions without docstrings, try/except without
  logging, `--ast-call`)
- `--explain-symbol`, `--explain-file`
- Cross-repo dependency analysis via `imports` edges

You lose:

- Semantic `--search` (queries like "find code that retries on
  failure" when the source doesn't literally contain those words)

For many codebases this is a reasonable trade-off, especially the first
time through a new repo.

## `sqlite-vec` fails to load

Symptoms:

```
warn: failed to load sqlite-vec: ...
warn: vec_embeddings table not created; FTS-only results
```

Two root causes:

### A. Platform without a pre-built wheel

`sqlite-vec` ships wheels for linux-x86_64, linux-aarch64, macOS, and
win64. On other platforms it falls back to source, which needs a C
compiler. Either install build tools or accept FTS-only.

### B. Python built without extension loading

Some distro-packaged Python builds disable
`sqlite3.enable_load_extension` for security. The indexer detects this,
warns on stderr, and continues without vec. If you need semantic
search, install a Python that has extension loading enabled:

- `pyenv install 3.12.x` (has it)
- python.org installer (has it)
- `conda` / `miniconda` (has it)
- Some Ubuntu `python3-*` packages (may not — `python3-apt` tooling
  in particular sometimes strips it)

Verify:

```bash
python -c "import sqlite3; c=sqlite3.connect(':memory:'); c.enable_load_extension(True); print('OK')"
```

If that prints `OK`, you're fine.

## Proxy setups

If your corp uses an explicit HTTP proxy:

```bash
export HTTPS_PROXY=http://proxy.corp:3128
export HTTP_PROXY=http://proxy.corp:3128
export NO_PROXY=localhost,127.0.0.1,.corp.internal
```

These are respected by both pip and `requests` (and therefore
`huggingface_hub` / `fastembed`).

## First-run hang on the indexer

The first run with embeddings enabled downloads a ~130 MB ONNX model
from HuggingFace. On slow links this can take several minutes. Look at
stderr — fastembed prints a `Fetching 5 files: ...` tqdm bar while it
downloads. If that bar isn't progressing, it's a network issue, not an
indexer bug.

To prove it's a network problem:

```bash
# does this return within a few seconds?
curl -I https://huggingface.co
```

If no, apply the cert / proxy fixes above.

## Still stuck?

1. Run with `--no-embeddings` to confirm the non-embedding path works.
   If that fails too, it's not a network / cert problem — re-read the
   error on stderr.
2. Upgrade to a newer patch release: `pip install -U fastembed sqlite-vec`.
3. Open an issue with the full stderr output and your platform
   (`python --version && uname -a`).
