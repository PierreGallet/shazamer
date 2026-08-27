import { For, Show, createResource, createSignal } from "solid-js";
import type { SoulseekCandidate, Track } from "../lib/api";
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
  const [searching, setSearching] = createSignal(false);
  const [queued, setQueued] = createSignal<Record<string, boolean>>({});
  const [error, setError] = createSignal("");

  async function searchSoulseek() {
    setSearching(true);
    setError("");
    try {
      const result = await api.soulseekSearch(props.track.artist, props.track.title);
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

  async function enqueue(candidate: SoulseekCandidate) {
    setQueued({ ...queued(), [candidate.full_path]: true });
    try {
      await api.soulseekDownload(candidate);
    } catch (err) {
      setQueued({ ...queued(), [candidate.full_path]: false });
      setError(err instanceof Error ? err.message : "Could not queue the download");
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
        </div>

        <Show when={sources()!.soulseek_configured}>
          <div class="acquire-p2p">
            <div class="row">
              {/* One click, best match, verified and tagged — the list below
                  is for when you want to overrule that choice. */}
              <GetTrack track={props.track} enabled={true} />
              <button
                class="btn btn-ghost btn-sm"
                onClick={searchSoulseek}
                disabled={searching()}
              >
                <Show when={searching()}><span class="spinner" /></Show>
                {searching() ? "Searching…" : "Show all candidates"}
              </button>
              <span class="tiny faint">
                Needs your own account and a shared folder
              </span>
            </div>

            <Show when={candidates()}>
              <div class="candidates">
                <For each={candidates()!.slice(0, 8)}>
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
                        {queued()[candidate.full_path] ? "Queued" : "Get"}
                      </button>
                    </div>
                  )}
                </For>
              </div>
            </Show>
          </div>
        </Show>
      </Show>

      <Show when={error()}>
        <div class="tiny" style={{ color: "var(--warn)" }}>{error()}</div>
      </Show>
    </div>
  );
}
