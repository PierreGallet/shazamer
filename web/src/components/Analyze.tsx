import { Show, createEffect, createSignal, onCleanup } from "solid-js";
import type { TaskState } from "../lib/api";
import { api, subscribeToTask } from "../lib/api";

/**
 * Start an analysis and watch it run.
 *
 * Progress arrives over Server-Sent Events, so the bar reflects real work as
 * it happens rather than a poll every second. The stages are meaningful: the
 * decode percentage is computed from samples actually decoded, and the
 * identification count is real probes completing.
 */

const STAGE_LABELS: Record<string, string> = {
  pending: "Queued",
  downloading: "Downloading",
  downloaded: "Downloaded",
  probing: "Reading file",
  decoding: "Analysing audio",
  identifying: "Identifying tracks",
  merging: "Merging results",
  features: "Detecting BPM and key",
  done: "Done",
  completed: "Done",
  error: "Failed",
  cancelled: "Cancelled",
};

const STAGE_ORDER = ["downloading", "decoding", "identifying", "features"];

interface Props {
  /** Task to resume watching, from the URL. Undefined on a fresh form. */
  taskId?: string;
  /** Fired as soon as a task exists, so the URL can name it. */
  onStarted: (taskId: string) => void;
  onComplete: (setId: string) => void;
}

export default function Analyze(props: Props) {
  const [url, setUrl] = createSignal("");
  const [task, setTask] = createSignal<TaskState | null>(null);
  const [error, setError] = createSignal("");
  const [uploadPct, setUploadPct] = createSignal<number | null>(null);
  const [dragging, setDragging] = createSignal(false);
  let disposer: (() => void) | null = null;
  let fileInput!: HTMLInputElement;

  onCleanup(() => disposer?.());

  /**
   * Resume the task named in the URL.
   *
   * Reloading during a twenty-minute analysis used to drop you back to an
   * empty form with no way back to the run. The task id is in the address
   * now, so the stream is simply re-attached — and if it finished while the
   * page was closed, the first frame carries the final state.
   */
  createEffect(() => {
    const id = props.taskId;
    if (!id || id === task()?.task_id) return;
    setError("");
    setUploadPct(null);
    watch(id);
  });

  function watch(taskId: string) {
    disposer?.();
    disposer = subscribeToTask(taskId, {
      onUpdate: (state) => {
        setTask(state);
        if (state.status === "error" && state.error) setError(state.error);
        // Handled here rather than only in onEnd: a task that finished while
        // the page was closed sends its final state in the very first frame
        // and the stream closes immediately, so waiting for `end` alone would
        // leave a completed analysis sitting on the progress bar.
        if (state.status === "completed" && state.set_id) {
          props.onComplete(state.set_id);
        }
      },
      onEnd: () => {
        const state = task();
        if (state?.status === "completed" && state.set_id) {
          props.onComplete(state.set_id);
        }
      },
      onError: (message) => setError(message),
    });
  }

  async function submitUrl(event: Event) {
    event.preventDefault();
    const value = url().trim();
    if (!value) {
      setError("Paste a YouTube, SoundCloud or Mixcloud link first.");
      return;
    }
    reset();
    try {
      const { task_id } = await api.analyzeUrl(value);
      props.onStarted(task_id);
      watch(task_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the analysis");
    }
  }

  async function submitFile(file: File) {
    reset();
    setUploadPct(0);
    try {
      const { task_id } = await api.analyzeUpload(file, setUploadPct);
      setUploadPct(null);
      props.onStarted(task_id);
      watch(task_id);
    } catch (err) {
      setUploadPct(null);
      setError(err instanceof Error ? err.message : "Upload failed");
    }
  }

  function reset() {
    setError("");
    setTask(null);
    setUploadPct(null);
    disposer?.();
    disposer = null;
  }

  async function cancel() {
    const state = task();
    if (!state) return;
    try {
      await api.cancelTask(state.task_id);
    } catch {
      /* the stream will report the real outcome */
    }
  }

  const running = () =>
    task() !== null && !["completed", "error", "cancelled"].includes(task()!.status);

  const busy = () => running() || uploadPct() !== null;

  return (
    <div class="wrap">
      <div class="analyze">
        <div class="analyze-hero">
          <h1>Find every track in a set</h1>
          <p class="muted">
            Paste a mix, get a timestamped tracklist with BPM and key — then go
            find the records.
          </p>
        </div>

        <form class="url-form" onSubmit={submitUrl}>
          <input
            class="input"
            type="url"
            placeholder="https://soundcloud.com/… or a YouTube link"
            value={url()}
            onInput={(e) => setUrl(e.currentTarget.value)}
            disabled={busy()}
          />
          <button class="btn btn-primary" type="submit" disabled={busy()}>
            Analyse
          </button>
        </form>

        <div class="or-line"><span>or</span></div>

        <div
          class="drop"
          classList={{ over: dragging() }}
          onClick={() => !busy() && fileInput.click()}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            const file = e.dataTransfer?.files?.[0];
            if (file && !busy()) void submitFile(file);
          }}
        >
          <svg viewBox="0 0 24 24" width="24" height="24" fill="none"
               stroke="currentColor" stroke-width="1.5" stroke-linecap="round"
               stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <path d="M17 8l-5-5-5 5M12 3v12" />
          </svg>
          <div class="drop-title">Drop an audio file</div>
          <div class="tiny faint">
            MP3, WAV, FLAC, M4A, OGG, Opus, AIFF — no length limit
          </div>
          <input
            ref={fileInput}
            type="file"
            hidden
            accept=".mp3,.wav,.flac,.m4a,.ogg,.opus,.aac,.wma,.aiff,.aif,.webm"
            onChange={(e) => {
              const file = e.currentTarget.files?.[0];
              if (file) void submitFile(file);
              e.currentTarget.value = "";
            }}
          />
        </div>

        <Show when={uploadPct() !== null}>
          <div class="progress">
            <div class="progress-head">
              <span>Uploading</span>
              <span class="mono">{uploadPct()!.toFixed(0)}%</span>
            </div>
            <div class="progress-track">
              <div class="progress-fill" style={{ width: `${uploadPct()}%` }} />
            </div>
          </div>
        </Show>

        <Show when={task()}>
          {(state) => (
            <div class="progress">
              <div class="progress-head">
                <span>
                  {STAGE_LABELS[state().stage] ?? state().stage}
                  <Show when={state().filename && state().filename !== "Resolving..."}>
                    <span class="faint"> · {state().filename}</span>
                  </Show>
                </span>
                <span class="mono">{state().progress}%</span>
              </div>
              <div class="progress-track">
                <div
                  class="progress-fill"
                  classList={{ failed: state().status === "error" }}
                  style={{ width: `${state().progress}%` }}
                />
              </div>
              <div class="progress-foot">
                <span class="tiny muted">{state().message}</span>
                <Show when={running()}>
                  <button class="btn btn-ghost btn-sm" onClick={cancel}>
                    Cancel
                  </button>
                </Show>
              </div>

              <div class="stage-track">
                {STAGE_ORDER.map((stage) => {
                  const reached = () =>
                    STAGE_ORDER.indexOf(state().stage) >= STAGE_ORDER.indexOf(stage) ||
                    state().status === "completed";
                  return (
                    <span
                      class="stage-dot"
                      classList={{
                        done: reached(),
                        now: state().stage === stage,
                      }}
                    >
                      {STAGE_LABELS[stage]}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </Show>

        <Show when={error()}>
          <div class="error-box">{error()}</div>
        </Show>
      </div>
    </div>
  );
}
