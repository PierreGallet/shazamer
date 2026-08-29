import { For, Show, createEffect, createResource, createSignal,
         onCleanup } from "solid-js";
import type { Download, SoulseekCandidate, Track } from "../lib/api";
import GetTrack from "./GetTrack";
import { api, formatBytes } from "../lib/api";

/**
 * Where to get one track.
 *
 * Stores come first, and not as a disclaimer: a bought file arrives with
 * correct tags and a catalogue number, and it pays the label you just found.
 * Soulseek appears only when you have pointed the app at your own slskd
 * instance, and it needs an account with real shares — the network runs on
 * reciprocity.
 */

interface Props {
  track: Track;
}

export default function AcquirePanel(props: Props) {
  const [sources] = createResource(
    () => ({ artist: props.track.artist, title: props.track.title }),
    (params) => api.acquisitionSources(params.artist, params.title),
  );

  const [candidates, setCandidates] = createSignal<SoulseekCandidate[] | null>(null);
  /**
   * What was actually asked of the network.
   *
   * Shown because it is the first thing worth knowing when a search comes
   * back thin: Soulseek matches a plain substring against filenames, so what
   * we send decides what can possibly come back — and it is not the title as
   * written. "(Extended Mix)" and ampersands are stripped, because leaving
   * them in costs the whole result rather than narrowing it.
   */
  const [query, setQuery] = createSignal("");
  const [searching, setSearching] = createSignal(false);
  const [queued, setQueued] = createSignal<Record<string, boolean>>({});
  const [error, setError] = createSignal("");

  let dialog: HTMLDialogElement | undefined;

  /**
   * The transfer we started, followed to the end.
   *
   * Followed because "Queued" and nothing else is the state this feature was
   * reported broken in: a transfer that had finished, one sitting behind
   * forty people in a peer's queue, and one whose peer never answered all
   * looked identical. Soulseek queues are genuinely slow, so the answer is to
   * say what is happening rather than to hurry it.
   */
  const [started, setStarted] = createSignal<number | null>(null);
  const [progress, setProgress] = createSignal<Download | null>(null);
  let timer: number | undefined;

  onCleanup(() => window.clearTimeout(timer));

  createEffect(() => {
    const id = started();
    if (id === null) return;
    const tick = async () => {
      try {
        const state = await api.download(id);
        setProgress(state);
        if (state.status !== "ready" && state.status !== "failed") {
          timer = window.setTimeout(tick, 3000);
        }
      } catch {
        // A blip should not abandon a transfer that may still be running.
        timer = window.setTimeout(tick, 6000);
      }
    };
    timer = window.setTimeout(tick, 500);
  });

  /** Open first, search second: twenty seconds of nothing happening after a
   *  click reads as a dead button. */
  function openPicker() {
    setCandidates(null);
    setQuery("");
    setError("");
    dialog?.showModal();
    void searchSoulseek();
  }

  async function searchSoulseek() {
    setSearching(true);
    setError("");
    try {
      const result = await api.soulseekSearch(props.track.artist, props.track.title);
      setQuery(result.query);
      setCandidates(result.candidates);
      if (result.candidates.length === 0) {
        setError("No peer is sharing this one right now. Try again later — the "
               + "pool changes constantly.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Soulseek search failed");
    } finally {
      setSearching(false);
    }
  }

  /**
   * Take one candidate, through the same path as the one-click button.
   *
   * The direct slskd call this used to make queued the transfer and stopped
   * there: the file arrived on the server, in slskd's own folder, with
   * nothing to move it, tag it or hand it to the browser. It downloaded
   * successfully and there was no way to get it — which is worse than
   * failing, because it looks like it worked.
   *
   * Going through /api/acquire/track gives the transfer a row to report
   * against, and the runner behind it verifies the audio, tags it, and puts
   * it where the download endpoint can serve it.
   */
  async function enqueue(candidate: SoulseekCandidate) {
    setQueued({ ...queued(), [candidate.full_path]: true });
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
        chosen: candidate,
      });
      setStarted(download_id);
      dialog?.close();
    } catch (err) {
      setQueued({ ...queued(), [candidate.full_path]: false });
      setError(err instanceof Error ? err.message : "Could not start the fetch");
    }
  }

  return (
    <div class="acquire">
      <div class="acquire-head">
        <span class="eyebrow">Where to get it</span>
        <span class="tiny faint">{props.track.artist} — {props.track.title}</span>
      </div>

      <Show when={sources()} fallback={<div class="tiny faint">Loading sources…</div>}>
        <div class="acquire-grid">
          <For each={sources()!.sources.filter((s) => !s.actionable)}>
            {(source) => (
              <a
                class="acquire-card"
                href={source.url}
                target="_blank"
                rel="noopener noreferrer"
              >
                <div class="acquire-card-top">
                  <span class="acquire-name">{source.name}</span>
                  <span
                    class="chip"
                    classList={{ "chip-accent": source.kind === "store" }}
                  >
                    {source.kind}
                  </span>
                </div>
                <div class="tiny muted">{source.quality}</div>
                <Show when={source.note}>
                  <div class="tiny faint">{source.note}</div>
                </Show>
              </a>
            )}
          </For>

          {/* In the same grid as the rest, because it is the same kind of
              thing: another place the record might be. It opens a dialog
              rather than a tab only because this one can hand you the file. */}
          <Show when={sources()!.soulseek_configured}>
            <button class="acquire-card acquire-card-action" onClick={openPicker}>
              <div class="acquire-card-top">
                <span class="acquire-name">Soulseek</span>
                <span class="chip chip-accent">p2p</span>
              </div>
              <div class="tiny muted">MP3 320 or lossless, from other people</div>
              <div class="tiny faint">Pick from what people are sharing</div>
            </button>
          </Show>
        </div>


        <dialog class="picker" ref={dialog} onClose={() => setCandidates(null)}>
          <div class="picker-head">
            <div>
              <div class="eyebrow">Soulseek</div>
              <div class="tiny faint">
                {props.track.artist} — {props.track.title}
              </div>
              <Show when={query()}>
                <div class="tiny faint">
                  searched for <span class="mono">{query()}</span>
                </div>
              </Show>
            </div>
            <button class="btn btn-ghost btn-sm" onClick={() => dialog?.close()}>
              Close
            </button>
          </div>

          <Show when={searching()}>
            {/* Said out loud because it is genuinely slow: peers answer over
                about twenty seconds and a silent spinner reads as broken. */}
            <div class="picker-wait">
              <span class="spinner" />
              <span class="tiny muted">
                Asking the network — this takes about twenty seconds.
              </span>
            </div>
          </Show>

          <Show when={!searching() && candidates()}>
            <Show
              when={candidates()!.length}
              fallback={
                <div class="tiny faint picker-empty">
                  Nobody is sharing this one right now. The pool changes
                  constantly, so it is worth trying again later.
                </div>
              }
            >
              <div class="candidates">
                <For each={candidates()!.slice(0, 5)}>
                  {(candidate) => (
                    <div class="candidate">
                      <span
                        class="chip"
                        classList={{ "chip-key": candidate.lossless }}
                      >
                        {candidate.quality_label}
                      </span>
                      <span class="candidate-file" title={candidate.full_path}>
                        {candidate.filename}
                      </span>
                      <span class="tiny faint mono">
                        {formatBytes(candidate.size)}
                      </span>
                      <span
                        class="tiny"
                        classList={{
                          faint: !candidate.free_slot,
                          muted: candidate.free_slot,
                        }}
                      >
                        {candidate.free_slot
                          ? "free slot"
                          : `queue ${candidate.queue_length}`}
                      </span>
                      <button
                        class="btn btn-ghost btn-sm"
                        disabled={queued()[candidate.full_path]}
                        onClick={() => enqueue(candidate)}
                      >
                        {queued()[candidate.full_path] ? "Queued" : "Download"}
                      </button>
                    </div>
                  )}
                </For>
              </div>
              <div class="picker-foot">
                {/* The one-click path, kept and explained: it applies the same
                    ranking as the list above and takes the top one. */}
                <GetTrack track={props.track} enabled={true} />
                <span class="tiny faint">
                  Ranked by format, bitrate and length — extended mixes first.
                </span>
              </div>
            </Show>
          </Show>
        </dialog>

      </Show>

      <Show when={progress()}>
        {(state) => (
          <div class="acquire-progress">
            <div class="row">
              <span
                class="chip"
                classList={{
                  "chip-accent": state().status === "ready",
                  "chip-warn": state().status === "failed",
                }}
              >
                {state().status}
              </span>
              <span class="tiny muted">{state().message}</span>
              <Show when={state().status === "ready"}>
                <a class="btn btn-primary btn-sm"
                   href={api.downloadFileUrl(state().id)}
                   download="">
                  Save the file
                </a>
              </Show>
            </div>
            <Show when={state().progress > 0 && state().status !== "ready"}>
              <div class="progress-track">
                <div class="progress-fill"
                     style={{ width: `${state().progress}%` }} />
              </div>
            </Show>
          </div>
        )}
      </Show>

      <Show when={error()}>
        <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
      </Show>
    </div>
  );
}
