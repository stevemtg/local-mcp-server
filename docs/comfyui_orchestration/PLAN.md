# Direct MCP image orchestration with Ollama and ComfyUI

This design adds `generate_image` to the existing Python MCP server. The host
keeps the conversation and tool call pending while the MCP process transfers
GPU use from Ollama to ComfyUI and back to the **same Ollama model**.

Deliverables in this directory:

| File | Purpose |
| --- | --- |
| [reference.py](reference.py) | Complete asynchronous handler, API clients, GPU lease, durable journal, recovery entry point, and MCP registration. |
| [workflow.example.json](workflow.example.json) | Small API-format graph with explicit text, sampler, and SaveImage node mappings. Replace its checkpoint with an installed compatible SD 1.5 checkpoint. |
| [test_reference.py](test_reference.py) | Offline failure-injection and MCP registration tests. |
| [requirements-reference.txt](requirements-reference.txt) | Reference dependencies, separate from existing runtime requirements. |

The reference is deliberately separate from `server.py`: this is an implementation
plan and reviewable implementation, not an enabled feature. Production deployment
requires the host coordination and hardware validation below. No real model was
unloaded and no real ComfyUI generation was run during this work.

## 1. API Lifecycle Breakdown

### Ownership and prerequisites

Use one dedicated ComfyUI instance and one CUDA GPU for the first deployment.
Every client that can cause GPU allocation must participate in the same lease:
the agent host, MCP image tools, indexing/search embeddings, and any background
jobs. An OS file lock coordinates processes; a durable unfinished-job record
keeps the gate closed after a process crash. This is a cooperative scheduler,
not a GPU-driver lock. Clients that bypass it invalidate the guarantee.

The host completes the Ollama response stream, records the assistant's complete
tool invocation, and releases its inference lease **before** calling MCP. An
inference lease held across tool execution causes a deadlock. Do not dispatch a
partially streamed tool call while Ollama is still generating.

MCP does not automatically tell a tool which model the host is using. Configure
the exact canonical chat model and load options from the host's configuration.
For multiple chat configurations, use a trusted host-to-server session mapping;
the language model must not choose the unload target. Do not infer it from the
first entry in `/api/ps` or from this repository's `EMBED_MODEL`.

The reference requires the configured chat model to be GPU-resident and rejects
other resident GPU models. CPU-only embedding models can remain loaded. Validate
the workflow, output directory, checkpoint, node mapping, disk capacity, and
backend versions at startup. Preflight checks happen before unloading; after
admission there is no further LLM inference in the transaction.

### Normal execution

Example local endpoints: Ollama `http://127.0.0.1:11434`, ComfyUI
`http://127.0.0.1:8188`. Keep these in trusted server settings.

| Step | API or action | Completion condition |
| --- | --- | --- |
| 1 | Acquire the shared GPU lease; inspect recovery journal. | No unfinished transaction; all earlier inference/embedding requests have drained. |
| 2 | ComfyUI `GET /queue`; Ollama `GET /api/ps`. | ComfyUI idle; exact chat model present. Snapshot name, digest, context length, GPU allocation, host options, and configured keep-alive policy. |
| 3 | Commit journal, then Ollama `POST /api/generate` with the unload body below. | Poll `/api/ps` until the original digest is absent. |
| 4 | ComfyUI `POST /free`; inspect `/system_stats`. | Remove old ComfyUI caches and verify the measured image-generation VRAM budget is available. |
| 5 | ComfyUI `POST /prompt` with the API-format graph and unique client ID. | Receive and durably store `prompt_id`. Queue acknowledgement is not image completion. |
| 6 | Poll ComfyUI `GET /history/{prompt_id}` asynchronously. | History reports successful completion; error history causes failure handling. |
| 7 | Resolve `outputs[save_node].images` against the configured output root. | Expected output files exist, are nonempty, and remain inside the root. |
| 8 | Confirm the owned prompt is no longer queued/running; ComfyUI `POST /free`. | Queue drained and measured memory release criteria pass. |
| 9 | Ollama `GET /api/tags`, then preload with `POST /api/generate`. | Original digest still installed; `/api/ps` confirms original model, context length, and adequate GPU placement. |
| 10 | Commit final result, release GPU lease, return MCP result. | Host can append the tool result and resume the conversation. |

Ollama's running-model API supplies residency information, including digest,
context length, and `size_vram`. It does not identify the calling agent or expose
its full load configuration. [Ollama running models](https://docs.ollama.com/api/ps)

Unload request:

```http
POST /api/generate
Content-Type: application/json

{"model":"YOUR_CANONICAL_MODEL:TAG","prompt":"","stream":false,"keep_alive":0}
```

Reload request, with the **host's actual** options rather than the example value:

```http
POST /api/generate
Content-Type: application/json

{"model":"YOUR_CANONICAL_MODEL:TAG","prompt":"","stream":false,"keep_alive":"10m","options":{"num_ctx":8192}}
```

Empty-prompt requests control model residency without generating a substitute
assistant response. A zero keep-alive requests unloading; a nonzero policy keeps
the preloaded model available. Keep-alive is an explicit deployment policy, not
a recovered original TTL. [Ollama model lifecycle](https://github.com/ollama/ollama/blob/main/docs/api.md),
[Ollama generate API](https://docs.ollama.com/api/generate)

ComfyUI submission body:

```json
{
  "prompt": {"...": "trusted API-format graph with prompt text substituted"},
  "client_id": "UUID_FOR_THIS_REQUEST",
  "extra_data": {"orchestration_id": "UUID_FOR_THIS_REQUEST"}
}
```

The graph comes from ComfyUI's API export. The UI canvas document containing
node positions is a different format. The reference patches known positive and
negative `CLIPTextEncode` nodes, `KSampler.seed`, and `SaveImage.filename_prefix`.
The agent supplies bounded text, not arbitrary executable workflows, checkpoint
paths, URLs, or output paths. Use a different vetted template and mapping for
Flux, SDXL, or other node arrangements.

The stable queue/history routes support asynchronous HTTP-only orchestration.
Read `/object_info` during workflow validation to inspect installed node types.
For processes without a shared filesystem, `/view` can retrieve an output using
its filename, subfolder, and type; stream it to a bounded temporary file and
atomically publish it in an MCP-owned directory. The reference instead returns
verified paths on the shared local filesystem.
[ComfyUI server routes](https://docs.comfy.org/development/comfyui-server/comms_routes)

### WebSocket progress option

The implementation uses asynchronous history polling every second. At one
active job per GPU, this is inexpensive and avoids making the WebSocket a
single point of failure. A production UI can add a WebSocket consumer without
changing completion or recovery semantics:

1. Open `ws://127.0.0.1:8188/ws?clientId=<client_id>` **before** submitting.
2. Consume messages concurrently with history polling. Buffer early events until
   the queue response establishes `prompt_id`; filter by that ID afterward.
3. Use `progress`, `executing`, and `execution_cached` for display. Ignore binary
   preview frames unless the UI explicitly needs them.
4. Treat `execution_error` and `execution_interrupted` as failure signals.
   `execution_success` or `executing` with `node: null` triggers a history read.
5. Do not return on `executed`: it is a node UI update, not proof that the full
   workflow has finished. History may appear just after an event.
6. On disconnect, continue polling the same job. Never resubmit because the
   progress stream failed. Close the consumer task/socket during cleanup.

These message distinctions come from ComfyUI's documented execution events.
[ComfyUI WebSocket messages](https://docs.comfy.org/development/comfyui-server/comms_messages)

### Memory release is a separate stage

The ComfyUI release request is:

```http
POST /free
Content-Type: application/json

{"unload_models":true,"free_memory":true}
```

HTTP 200 acknowledges a request to the worker; it does not prove release has
finished. Current ComfyUI sets queue flags that its worker later processes.
Therefore, drain execution before freeing memory and wait for a measured
condition before loading Ollama. [ComfyUI route implementation](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py),
[ComfyUI worker implementation](https://github.com/Comfy-Org/ComfyUI/blob/master/main.py)

For the CUDA implementation, `/system_stats.vram_free` includes memory reusable
inside PyTorch's reservation. Subtract `torch_vram_free` to estimate memory
available to another process. Require that free budget plus a low
`torch_vram_total` for three consecutive samples. This is a resource-availability
test, not an acknowledgement token. Calibrate it against the installed version
and driver; use a verified NVML/driver probe when these counters are unsuitable.
[ComfyUI memory accounting](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy/model_management.py)

The default 256 MiB idle reservation and 512 MiB reload headroom are starting
configuration values, not universal guarantees. Measure image peak allocation,
LLM weights plus KV/cache allocation, and ordinary display/process overhead.
The first release gates diffusion; the second gates LLM restoration. A fixed
sleep cannot establish that either allocation will fit. For multiple GPUs,
record the allocation and budget for each device and acquire leases in a fixed
order. This reference rejects unsupported device probes rather than assuming
CUDA semantics for MPS, DirectML, or shared-memory devices.

## 2. MCP Tool Implementation

### Code structure and execution behavior

Use [reference.py](reference.py) as the implementation source. It includes real
Ollama/ComfyUI HTTP calls and MCP registration; no image generation adapters are
left as pseudocode. `Settings`, `Journal`, `file_lease`, `gpu_session`, and
`Orchestrator` can be split into modules when integrated.

The core execution structure is:

```python
# Abbreviated control flow; reference.py contains the full executable handler.
async with shared_gpu_lease:
    snapshot = await capture_original_model()
    await commit_recovery_record(snapshot)
    try:
        await unload_original_model(snapshot)
        await ensure_comfy_memory_budget()
        prompt_id = await submit_once(workflow)
        history = await await_history_completion(prompt_id)
        image_paths = await verify_output_files(history)
    except cancellation_exception:
        record_cancellation()
        raise
    except TimeoutError:
        record_timeout()
    except generation_exception:
        record_generation_failure()
    finally:
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(cleanup_budget):
                await cancel_or_drain_owned_job()
                await release_comfy_memory()
                await reload_and_verify_original_model(snapshot)
            await commit_final_or_recovery_required_state()
    return standard_mcp_result
```

HTTP uses a lifespan-owned `httpx.AsyncClient`. Waiting uses AnyIO timers and
sleep, so requests yield to the event loop. SQLite commits and filesystem
verification run through `anyio.to_thread.run_sync`; these workers are not
abandoned during cancellation. There are no blocking `requests`, `time.sleep`,
or subprocess calls in the handler's event loop. Do not instantiate a new MCP
server or replace the agent's model during the tool call.

The reference targets the repository's official MCP Python **v2** API:
`MCPServer`, `ToolAnnotations`, and `CallToolResult`. Python constructor fields
use `structured_content` and `is_error`; serialization produces
`structuredContent` and `isError`. The registration test checks this conversion.
Operational failures return a tool execution error instead of pretending that
an arbitrary `{"status":"failed"}` dictionary alone sets the MCP error flag.
[MCP tools and results](https://modelcontextprotocol.io/specification/2025-11-25/server/tools),
[Python SDK structured results](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/advanced/low-level-server.md)

Tool inputs:

| Parameter | Contract |
| --- | --- |
| `prompt` | Required nonblank text; at most 8,000 characters. |
| `negative_prompt` | Optional text; at most 4,000 characters. |
| `request_id` | Optional UUID; host should assign and persist it before dispatch. Reuse only for retries with identical inputs. |

The unique ID determines the example seed and output prefix. A journalled
fingerprint binds a request ID to the graph, prompt, model, and load options.
Completed retries replay their stored response without generating again;
conflicting reuse fails. An unfinished transaction blocks further GPU work.
Because callers can omit the ID, the tool advertises `idempotent_hint=False`.
Stored replay fields describe the original execution; the host still performs
normal residency checks when it next obtains an inference lease.

Example tool result payload after success:

```json
{
  "status": "succeeded",
  "request_id": "3ab15f90-2a13-4509-bf0f-d340fa850025",
  "prompt_id": "676f14fb-1afb-4e18-a016-596dd0ce5b34",
  "image_path": "E:\\ComfyUI\\output\\mcp\\3ab15f90-2a13-4509-bf0f-d340fa850025_00001_.png",
  "image_paths": ["E:\\ComfyUI\\output\\mcp\\3ab15f90-2a13-4509-bf0f-d340fa850025_00001_.png"],
  "llm_restored": true,
  "error": null,
  "recovery_error": null
}
```

`CallToolResult` includes this payload as structured content and as a JSON text
content block for clients that consume text only. Success requires verified
output and successful restoration. A local path allows the agent to reference
the image; it does not make image pixels visible to the model. Add a separate
resource/read-image facility if the host needs visual inspection.

### Integrating with this repository

The current server exposes five synchronous tools and imports `MCPServer` from
`mcp.server.mcpserver`. Its `OllamaEmbedder` sends requests through a worker pool
and uses a separately configured embedding model. Extend those actual entry
points rather than introducing a parallel MCP transport.

| Integration target | Planned change |
| --- | --- |
| `server.py` | Add a lifespan for the shared HTTP client, initialize the orchestrator, and call `register(server, service)` to advertise `generate_image`. Keep stdout exclusively for MCP. |
| New orchestration package | Move the reference components here; preserve injected settings/client for testing. |
| `semantic_index/config.py` or new image configuration | Separate chat model, chat runner URL, workflow path, output root, shared GPU state root, load options, budgets, and deadlines from `EMBED_MODEL`. |
| `index_codebase`, `semantic_search`, `semantic_search_similar` | If they use this GPU, acquire `gpu_session` around the whole operation and await the existing synchronous indexer in a worker. Preserve signatures and schemas. Do not take the lock again inside every worker thread. |
| Agent host / Ollama gateway | Acquire the same lease and check the journal for each GPU inference request; release before dispatching tools. Suppress speculative/parallel GPU work while waiting. |
| Startup/supervisor | Run `recover()` under exclusive ownership before admitting GPU clients; retain blocked state on recovery failure. |
| Runtime requirements and tests | Add direct AnyIO/HTTPX dependencies, lock the tested MCP version, and run existing offline plus stdio tests after integration. |

For initial deployment, put semantic embeddings on a separately configured
CPU-only runner and verify zero GPU residency. If embeddings stay on this GPU,
extend the snapshot policy to explicitly manage their resident model too. The
reference currently **rejects** that case; adding an image-tool lock alone to
the existing server is insufficient. A stock host that cannot participate in
the lease needs a local Ollama gateway implementing the gate, or an enforced
single-client deployment with all other GPU work disabled.

Lifespan wiring sketch, once `settings` has been loaded and validated:

```python
from contextlib import asynccontextmanager
import httpx
from mcp.server.mcpserver import MCPServer
from image_orchestration import Orchestrator, register

@asynccontextmanager
async def lifespan(server):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10, connect=3),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        service = Orchestrator(settings, client)
        await service.recover()  # Keep GPU admission closed if this raises.
        register(server, service)
        yield {"image_orchestrator": service}

server = MCPServer("codebase-semantic-index", lifespan=lifespan)
# Preserve existing tool registration and server.run(transport="stdio").
```

Use the same lifecycle for recovery and client shutdown. If startup recovery
can take minutes, configure the host's startup timeout accordingly or move it
to a supervised service that keeps GPU admission closed while MCP exposes
CPU-only tools. Do not reset a dirty journal just to make startup succeed.

Runtime settings must be operator-owned. Use an absolute shared state directory
on a local filesystem; all cooperating processes must resolve the identical
lock path. Do not put the coordination database on an unreliable network share.
Keep ComfyUI output retention longer than the host's retry/reference window;
stored paths cannot guarantee that an operator never deletes a file later.

## 3. State & Context Preservation

### What survives the VRAM swap

The host owns the conversation, tool definitions, assistant tool invocation,
provider-specific tool-call identifiers, pending MCP request, and final result.
Persist these in ordinary RAM plus the host's existing durable conversation
store. The MCP journal owns only orchestration recovery state and artifact
references. It stores no prompt text or conversation transcript.

Unloading removes model residency and may discard KV/prefix caches. Those
caches are an optimization; conversation messages must remain authoritative.
After the tool returns, the host supplies the retained history and tool result
to the same model and permits normal prompt re-evaluation. A warm-up request
does not recreate the conversation or restore a previous KV cache.

```mermaid
sequenceDiagram
    participant H as Agent host
    participant O as Ollama
    participant M as MCP orchestrator
    participant C as ComfyUI
    H->>O: Chat with history and tool definitions
    O-->>H: Complete assistant tool call; stream ends
    Note over H: Persist history; release inference lease
    H->>M: tools/call generate_image
    M->>M: Acquire GPU lease; journal original model
    M->>O: Unload original model
    M->>C: Free stale caches; queue workflow
    loop Asynchronous wait
        M->>C: Read history for prompt_id
        C-->>M: Pending, failure, or saved outputs
    end
    M->>C: Drain job; free memory; verify budget
    M->>O: Reload original model and load options
    O-->>M: Residency verified
    M-->>H: Tool result with status and local path
    Note over H: Append matching tool result; acquire inference lease
    H->>O: Resume using retained conversation
```

The pending MCP JSON-RPC request ID, the host/provider tool-call ID, the
application idempotency UUID, and ComfyUI's prompt ID serve different purposes.
Keep an explicit mapping. Reusing an MCP request ID is not an idempotency
mechanism. When cancellation disconnects the original request, retain the
application UUID so the host can retrieve/replay the settled result later.

There is no mid-flight model switch, second model, recursive agent call, or
LLM-based recovery step. The MCP process, host, and supervisor are CPU programs
and can continue operating while model weights are absent from VRAM.

### Host timeouts and progress

The reference defaults to 10 seconds for lease acquisition, 90 for unloading,
600 for generation, and a separately shielded 300-second cleanup budget.
Within cleanup it caps queue draining at 30 seconds, memory release at 30, and
reload at 180. The pre-generation memory check adds up to 30 seconds. Allow
approximately 20 minutes for the overall tool call, with a separately measured
startup budget; tune these from actual cold-load and generation timings.

Generation timeout and recovery timeout are independent. The host must not
force-kill the MCP process at the generation deadline. Optional progress
notifications can show phases and node progress when the negotiated protocol
and client support them. Use the request's progress token, increasing progress
values, and a final completion notification only after restoration. Notifications
do not universally extend a client's timeout; configure its deadline explicitly.
[MCP progress](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/progress)

On cancellation, stop the owned generation and complete bounded recovery before
letting cancellation propagate. The original requester may no longer receive a
result, so persist the settled status for host recovery. Shielding protects
against cooperative cancellation, not process termination or power loss.
[MCP cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)

## 4. Robust Error Handling

### Failure policy

| Failure | Handler behavior | Host action |
| --- | --- | --- |
| Invalid arguments, workflow mapping, resident model, or busy queue | Fail before unloading. | Correct input/configuration or wait for the current owner. |
| Ollama unload fails or times out | Do not submit an image; attempt restoration from the saved snapshot. | Continue only after confirmed restoration; investigate unresolved runner operations. |
| ComfyUI rejects workflow with HTTP 400 | Mark submission as conclusively rejected; free memory and restore Ollama. | Fix the vetted workflow; preserve useful sanitized node diagnostics in operational logs. |
| ComfyUI records execution failure/OOM | Record generation error; drain, release ComfyUI memory, and reload original model. | Receive `isError:true` with `llm_restored:true` if recovery succeeded. |
| History GET temporarily fails | Retry reads within the generation deadline. | No duplicate generation. |
| Generation times out | Remove owned pending entry or interrupt owned running job; poll until drained. | Receive timeout result after restoration. |
| MCP client cancels | Run shielded cleanup, persist status, propagate cancellation. | Reconcile by application UUID if the response was lost. |
| Output missing, empty, or escapes root | Do not return an unverified path; restore the runner. | Treat as output failure. |
| Image saved but reload fails | Preserve image paths and generation outcome; store separate recovery error. | Surface artifact and recovery state without needing LLM inference. |
| ComfyUI refuses to stop/free or probe never reaches budget | Do not load Ollama into potentially occupied VRAM. Leave durable recovery block. | Supervisor must resolve backend/GPU state. |
| Model tag changed, model missing, context mismatch, or GPU placement regressed | Refuse to claim original-model restoration. | Restore the original artifact/configuration and rerun supervised recovery. |
| Process dies after unloading | OS lease is released, dirty journal remains. | Block new GPU clients, then run startup/supervisor recovery. |

During cancellation the reference uses `POST /queue` with only the owned prompt
ID, and `POST /interrupt` for the running owned ID. Newer ComfyUI implementations
support a `prompt_id` on `/interrupt`; older releases may interrupt globally.
This is one reason the implementation requires a dedicated instance and an
exclusive submission path. It never clears a shared queue. Pin and validate
the installed cancellation behavior. [ComfyUI interruption implementation](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py)

### Ambiguous submission is a distinct distributed-systems failure

A timeout from `POST /prompt` can occur after the request has reached ComfyUI.
It is unsafe to assume the job was rejected, automatically repeat the POST, or
assume one empty queue response proves a delayed submission cannot appear.
The reference journals `submission_unknown` **before** submission. It clears
that flag only after receiving a usable job ID or a conclusive validation
rejection. If the acknowledgement is lost, it returns `recovery_required` and
leaves GPU admission closed. This intentionally favors controlled recovery over
an overlapping LLM reload.

Supervisor recovery for this state must establish a barrier against late
acceptance: reconcile the request's `client_id`/`orchestration_id` with queue and
history **and** ensure no submission handler remains in flight. A dedicated
gateway can implement an acknowledged submission transaction. Alternatively,
an operator-authorized restart of the exact dedicated ComfyUI process, followed
by confirmed exit of its workers and CUDA allocations, establishes that old
requests cannot execute. Preserve existing output files. Reconcile the journal
under the same lease, retain the generation error, and clear uncertainty only
after recording that evidence. Then free/check memory and reload Ollama.

`recover()` handles known-job recovery and failed reloads, but deliberately
refuses this ambiguous case until the supervisor has established that barrier.
It never silently restarts arbitrary local processes. A production supervisor
implementation is deployment-specific work in the rollout below. Apply the
same reasoning to unresolved runner lifecycle requests; a health/readiness
probe cannot prove ordering of a still-running remote mutation.

### Recovery failure is observable without a functioning LLM

Return `isError:true`, `status:"recovery_required"`, saved `image_paths` where
available, `llm_restored:false`, and separate `error` / `recovery_error` fields.
Keep the journal incomplete so cooperating hosts reject inference and future
image generation. Expose this state in the host UI or supervisor health endpoint
and log request ID, phase, durations, backend status, and restoration attempts
to stderr. Avoid logging full prompts or large backend bodies.

No tool can guarantee automatic return to service after a dead GPU driver,
unavailable runner, missing model, or process kill. The correct contract is
bounded automatic restoration for recoverable failures and an explicit blocked
state for failures that need a supervisor. The host must not depend on the
unloaded LLM to interpret or execute the recovery procedure.

### Verification and rollout

The supplied **13 offline tests pass** with Python 3.13, MCP 2.2.0, HTTPX 0.28.1,
and AnyIO 4.15.1. They exercise real handler control flow with an HTTPX mock
transport: normal ordering, replay/conflict, generation failure, timeout and
interrupt, cancellation shielding, failed reload and recovery, uncertain
submission, validation rejection, output-path rejection, busy preflight,
context mismatch, lock exclusion/acquisition timeout, and MCP registration and
serialized result fields. Mock output bytes establish file handling only;
these tests do not validate image quality or actual VRAM release.

Run the reference tests in an environment with the reference dependencies:

```powershell
python -m unittest discover -s docs\comfyui_orchestration -p test_reference.py -v
```

Implement and deploy in this order:

1. **Pin and measure.** Record Ollama and ComfyUI versions/commit, GPU and driver,
   exact chat-model digest, load options, vetted workflow hash, and checkpoint.
   Measure cold load, image peak VRAM, idle ComfyUI allocation, and reload time.
   Confirm the configured output root is the one ComfyUI actually uses.
2. **Coordinate admission.** Implement host/gateway and embedding participation
   in the shared lease and recovery gate. Exercise another host request and an
   indexing request during generation; neither may allocate this GPU. Separate
   CPU embeddings or implement explicitly managed secondary GPU-model snapshots.
3. **Integrate the handler.** Move the reference into the runtime package, add
   configuration/lifespan/registration, and keep existing tool contracts intact.
   Add sanitized node diagnostics, operational phase metrics, journal retention,
   and optional WebSocket-to-MCP progress if the client needs it.
4. **Integrate supervision.** Implement process ownership checks, dirty-journal
   startup recovery, health reporting, and the acknowledged submission barrier
   or tightly scoped restart procedure. Do not delete recovery records on boot.
5. **Run hardware acceptance tests.** Prove unload-before-image and
   free-before-reload with driver telemetry; verify original digest, context
   length, and GPU placement. Confirm a valid image is saved and the host resumes
   with the correct history/tool-call association.
6. **Inject real failures.** Invalid checkpoint, OOM, stalled custom node,
   disconnected WebSocket/history, lost queue acknowledgement, MCP cancellation,
   backend disconnect, model-tag replacement, and MCP process death after each
   journal phase. Verify recovery blocks new work whenever state is uncertain.
7. **Run repository regressions.** Existing offline suite and MCP stdio
   round-trip tests after runtime integration. Verify logs never corrupt stdout,
   all existing tool schemas remain intact, and read-only tools still work.

Release only after those gates pass on the deployment hardware. The reference
provides the handler and API behavior; actual client coordination, supervisor
policy, workflow compatibility, and VRAM budgets determine production readiness.
