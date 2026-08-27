import { Show, createSignal, onCleanup } from "solid-js";
import type { Download, Track } from "../lib/api";
import { api } from "../lib/api";

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

  async function start(event: MouseEvent) {
    event.stopPropagation();
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
      });
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
            onClick={start}
            disabled={starting()}
            title="Find the best Soulseek match and fetch it"
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
              <button class="btn btn-ghost btn-sm" onClick={start}>
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

      <Show when={error()}>
        <span class="tiny" style={{ color: "var(--crit)" }}>{error()}</span>
      </Show>
    </Show>
  );
}
