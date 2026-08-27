import { For, Show, createSignal, onCleanup } from "solid-js";
import type { Download, SoulseekCandidate, Track } from "../lib/api";
import { api, formatBytes } from "../lib/api";

/**
 * Fetch one track from Soulseek.
 *
 * The button reports what is actually happening rather than spinning: a
 * Soulseek transfer can sit behind forty other people in a peer's queue, and
 * "queued" for ten minutes is a normal outcome that should not look like a
 * hang. Failures say what went wrong, because "nobody is sharing this" and
 * "the peer vanished at 60%" call for different responses.
 */

interface Props {
  track: Track;
  /** False when the server has no slskd configured. */
  enabled: boolean;
}

const POLL_MS = 3000;

export default function GetTrack(props: Props) {
  const [download, setDownload] = createSignal<Download | null>(null);
  const [candidates, setCandidates] = createSignal<SoulseekCandidate[] | null>(null);
  const [error, setError] = createSignal("");
  const [starting, setStarting] = createSignal(false);
  let timer: number | undefined;

  onCleanup(() => window.clearTimeout(timer));

  function follow(id: number) {
    const tick = async () => {
      try {
        const state = await api.download(id);
        setDownload(state);
        if (state.status !== "ready" && state.status !== "failed") {
          timer = window.setTimeout(tick, POLL_MS);
        }
      } catch {
        // A blip should not abandon a transfer that may still be running.
        timer = window.setTimeout(tick, POLL_MS * 2);
      }
    };
    timer = window.setTimeout(tick, 500);
  }

  /** Search and show what is out there, rather than picking blind. */
  async function look(event: MouseEvent) {
    event.stopPropagation();
    setStarting(true);
    setError("");
    try {
      const found = await api.acquireCandidates(
        props.track.artist, props.track.title);
      if (found.candidates.length === 0) {
        setError("Nobody is sharing this right now. The pool changes "
               + "constantly — worth trying again later.");
        return;
      }
      setCandidates(found.candidates);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setStarting(false);
    }
  }

  async function fetchOne(chosen?: SoulseekCandidate) {
    setStarting(true);
    setError("");
    try {
      const { download_id } = await api.acquireTrack({
        key: props.track.key,
        artist: props.track.artist,
        title: props.track.title,
        label: props.track.label,
        year: props.track.year,
        album: props.track.album,
        genre: props.track.genre,
        chosen,
      });
      setCandidates(null);
      follow(download_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the fetch");
    } finally {
      setStarting(false);
    }
  }

  const state = () => download();
  const running = () =>
    state() !== null && !["ready", "failed"].includes(state()!.status);

  return (
    <Show
      when={props.enabled}
      fallback={
        <span
          class="tiny faint"
          title="Set SLSKD_URL on the server to enable Soulseek"
        >
          —
        </span>
      }
    >
      <Show
        when={state()}
        fallback={
          <button
            class="btn btn-ghost btn-sm"
            onClick={look}
            disabled={starting()}
            title="Search Soulseek and show what is out there"
          >
            <Show when={starting()}><span class="spinner" /></Show>
            Get
          </button>
        }
      >
        {(current) => (
          <div class="get-state" onClick={(e) => e.stopPropagation()}>
            <Show when={current().status === "ready"}>
              <a
                class="btn btn-primary btn-sm"
                href={api.downloadFileUrl(current().id)}
                download=""
                title={
                  current().verified
                    ? `Fingerprint-checked · ${current().quality}`
                    : `Not fingerprint-checked · ${current().quality}`
                }
              >
                Save{current().verified ? "" : " ⚠"}
              </a>
              <span class="tiny faint">{current().quality}</span>
            </Show>

            <Show when={running()}>
              <span class="spinner" />
              <span class="tiny muted">{current().message}</span>
            </Show>

            <Show when={current().status === "failed"}>
              <button class="btn btn-ghost btn-sm" onClick={look}>
                Retry
              </button>
              <span class="tiny" style={{ color: "var(--warn)" }}
                    title={current().message}>
                {current().message.slice(0, 46)}
              </span>
            </Show>
          </div>
        )}
      </Show>

      <Show when={candidates()}>
        <div class="candidate-picker" onClick={(e) => e.stopPropagation()}>
          <div class="row tiny faint">
            <span>Best matches — the top one is what we would take</span>
            <div class="spacer" />
            <button class="btn-icon" onClick={() => setCandidates(null)}
                    title="Close">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none"
                   stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          <For each={candidates()!}>
            {(candidate, index) => (
              <button
                class="candidate-row"
                classList={{ best: index() === 0 }}
                onClick={() => fetchOne(candidate)}
                disabled={starting()}
              >
                {/* Duration first: it is what separates the extended mix from
                    the radio edit, and it is the thing most worth checking. */}
                <span class="chip mono"
                      classList={{ "chip-accent": (candidate.length ?? 0) > 360 }}>
                  {candidate.duration_label}
                </span>
                <span class="chip"
                      classList={{ "chip-key": candidate.lossless }}>
                  {candidate.quality_label}
                </span>
                <span class="candidate-name" title={candidate.full_path}>
                  {candidate.filename}
                </span>
                <span class="tiny faint mono">{formatBytes(candidate.size)}</span>
                <span class="tiny"
                      classList={{ faint: !candidate.free_slot,
                                   muted: candidate.free_slot }}>
                  {candidate.free_slot ? "free" : `queue ${candidate.queue_length}`}
                </span>
              </button>
            )}
          </For>
        </div>
      </Show>

      <Show when={error()}>
        <span class="tiny" style={{ color: "var(--crit)" }}>{error()}</span>
      </Show>
    </Show>
  );
}
