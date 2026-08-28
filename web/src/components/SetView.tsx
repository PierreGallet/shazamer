import { useSearchParams } from "@solidjs/router";
import { For, Show, createEffect, createResource, createSignal } from "solid-js";
import { api, formatDuration, formatTime } from "../lib/api";
import Player from "./Player";
import TrackList from "./TrackList";
import Waveform from "./Waveform";

/** One analysed set: waveform, transport, tracklist and exports. */

interface Props {
  setId: string;
  onBack: () => void;
  onReanalyse: (taskId: string) => void;
}

/** Stage names as a person would say them, not as the pipeline names them. */
const STAGE_NAMES: Record<string, string> = {
  probing: "Reading the file",
  downloading: "Downloading",
  decoding: "Analysing audio",
  identifying: "Identifying tracks",
  merging: "Merging results",
  confirming: "Confirming weak matches",
  features: "Detecting BPM and key",
};

const EXPORTS: { fmt: string; label: string; hint: string }[] = [
  { fmt: "rekordbox", label: "Rekordbox XML", hint: "Playlist with BPM and key filled in" },
  { fmt: "txt", label: "Text", hint: "The classic tracklist" },
  { fmt: "csv", label: "CSV", hint: "Spreadsheet with every field" },
  { fmt: "m3u", label: "M3U", hint: "Playlist file" },
  { fmt: "json", label: "JSON", hint: "Everything, raw" },
];

export default function SetView(props: Props) {
  const [detail] = createResource(() => props.setId, api.set);
  // ?t=<seconds> deep-links into a moment. Sharing "the bit at 47 minutes" is
  // most of what a set is worth passing around for.
  const [searchParams, setSearchParams] = useSearchParams<{ t?: string }>();
  const initialStart = Math.max(0, Number(searchParams.t) || 0);
  const [currentTime, setCurrentTime] = createSignal(initialStart);
  const [activeIndex, setActiveIndex] = createSignal<number | null>(null);
  const [playerError, setPlayerError] = createSignal("");
  const [showTiming, setShowTiming] = createSignal(false);
  const [reanalyseUrl, setReanalyseUrl] = createSignal("");
  const [reanalyseError, setReanalyseError] = createSignal("");
  const [reanalysing, setReanalysing] = createSignal(false);

  async function reanalyse(event: Event) {
    event.preventDefault();
    const url = reanalyseUrl().trim();
    if (!url) {
      setReanalyseError("Paste the link this set came from.");
      return;
    }
    setReanalysing(true);
    setReanalyseError("");
    try {
      const { task_id } = await api.analyzeUrl(url, props.setId);
      props.onReanalyse(task_id);
    } catch (err) {
      setReanalysing(false);
      setReanalyseError(
        err instanceof Error ? err.message : "Could not start the analysis",
      );
    }
  }
  const [audio, setAudio] = createSignal<HTMLAudioElement | null>(null);

  // Follow playback: whichever segment contains the playhead becomes active.
  createEffect(() => {
    const data = detail();
    if (!data) return;
    const time = currentTime();
    const index = data.tracks.findIndex((t) => time >= t.start && time < t.end);
    if (index !== -1 && index !== activeIndex()) setActiveIndex(index);
  });

  /**
   * Seek, even before the audio element is ready.
   *
   * Assigning `currentTime` on an element that has not loaded metadata is
   * silently ignored, which made a click on the waveform move the playhead
   * while the transport stayed at 0:00. Pending seeks are replayed once the
   * element reports it can seek.
   */
  function seek(time: number) {
    setCurrentTime(time);
    // replace, not push: scrubbing must not fill the history stack with every
    // position you passed through.
    setSearchParams({ t: time > 0 ? String(Math.round(time)) : undefined },
                    { replace: true });
    applyToAudio(time);
  }

  /** Position the element, waiting for metadata if it is not ready yet. */
  function applyToAudio(time: number) {
    const element = audio();
    if (!element) return;
    if (element.readyState >= 1) {
      element.currentTime = time;
    } else {
      element.addEventListener(
        "loadedmetadata",
        () => { element.currentTime = time; },
        { once: true },
      );
    }
  }


  return (
    <div class="wrap wrap-wide">
      <Show when={detail.loading}>
        <div class="row"><span class="spinner" /> <span class="muted">Loading set…</span></div>
      </Show>

      <Show when={detail.error}>
        <div class="error-box">Could not load this set.</div>
      </Show>

      <Show when={detail()}>
        {(data) => (
          <>
            <div class="set-head">
              <button class="btn btn-ghost btn-sm" onClick={props.onBack}>
                ← Library
              </button>
              <div class="set-title-block">
                <h1>{data().title}</h1>
                <div class="row-wrap tiny faint">
                  <span>{formatDuration(data().duration)}</span>
                  <span>·</span>
                  <span>
                    <strong class="muted">{data().stats.identified ?? 0}</strong> identified
                  </span>
                  <Show when={data().stats.unidentified}>
                    <span>·</span>
                    <span>{data().stats.unidentified} unidentified</span>
                  </Show>
                  <Show when={data().stats.coverage != null}>
                    <span>·</span>
                    <span>{Math.round((data().stats.coverage ?? 0) * 100)}% covered</span>
                  </Show>
                  <Show when={data().quality}>
                    <span>·</span>
                    <span class="mono">{data().quality}</span>
                  </Show>
                  <Show when={data().stats.elapsed_seconds}>
                    <span>·</span>
                    <button
                      class="timing-toggle"
                      onClick={() => setShowTiming(!showTiming())}
                      title="Where the time went"
                    >
                      analysed in {formatTime(data().stats.elapsed_seconds!)}
                    </button>
                  </Show>
                </div>
              </div>
              <div class="spacer" />
              <div class="export-menu">
                <For each={EXPORTS}>
                  {(item) => (
                    <a
                      class="btn btn-ghost btn-sm"
                      href={api.exportUrl(props.setId, item.fmt)}
                      title={item.hint}
                      download=""
                    >
                      {item.label}
                    </a>
                  )}
                </For>
              </div>
            </div>

            <Show when={showTiming() && data().stats.stage_seconds}>
              {/* The stages are wildly unequal and which one dominates moves
                  with the set and with how the fingerprinting service is
                  behaving. Finding that out used to mean reading logs. */}
              <div class="timing">
                <For each={Object.entries(data().stats.stage_seconds!)
                                 .filter(([, s]) => s >= 0.05)}>
                  {([stage, seconds]) => {
                    const total = Object.values(
                      data().stats.stage_seconds!).reduce((a, b) => a + b, 0);
                    const share = total ? (seconds / total) * 100 : 0;
                    return (
                      <div class="timing-row">
                        <span class="timing-stage">{STAGE_NAMES[stage] ?? stage}</span>
                        <div class="timing-bar">
                          <div class="timing-fill" style={{ width: `${share}%` }} />
                        </div>
                        <span class="tiny faint mono">{formatTime(seconds)}</span>
                        <span class="tiny faint mono">{share.toFixed(0)}%</span>
                      </div>
                    );
                  }}
                </For>
              </div>
            </Show>

            <Show when={data().source_kind === "legacy"}>
              <div class="legacy-banner">
                <div>
                  <strong>Imported from an old tracklist.</strong>{" "}
                  <span class="muted">
                    No audio, waveform, BPM or key — none of it was recorded
                    back then. Paste the link this set came from to re-analyse
                    it in place.
                  </span>
                </div>
                <form class="url-form" onSubmit={reanalyse}>
                  <input
                    class="input input-sm"
                    type="url"
                    placeholder="https://youtube.com/… or a SoundCloud link"
                    value={reanalyseUrl()}
                    onInput={(e) => setReanalyseUrl(e.currentTarget.value)}
                    disabled={reanalysing()}
                  />
                  <button class="btn btn-primary btn-sm" type="submit"
                          disabled={reanalysing()}>
                    <Show when={reanalysing()}><span class="spinner" /></Show>
                    Re-analyse
                  </button>
                </form>
                <Show when={reanalyseError()}>
                  <div class="tiny" style={{ color: "var(--crit)" }}>
                    {reanalyseError()}
                  </div>
                </Show>
              </div>
            </Show>

            <div class="set-stage-sticky">
            <div class="panel panel-flush set-stage">
              <Waveform
                waveform={data().waveform}
                tracks={data().tracks}
                duration={data().duration}
                currentTime={currentTime()}
                activeIndex={activeIndex()}
                onSeek={seek}
                onSelect={setActiveIndex}
              />

              <Show
                when={data().has_audio}
                fallback={
                  <div class="player player-absent tiny faint">
                    <Show
                      when={data().source_kind === "legacy"}
                      fallback={
                        <>
                          Audio for this set has been cleared. The tracklist and
                          waveform remain — re-run the source URL to play it again.
                        </>
                      }
                    >
                      No audio for an imported set — it was never kept. Paste the
                      source link above to get it.
                    </Show>
                  </div>
                }
              >
                <Player
                  src={api.audioUrl(props.setId)}
                  duration={data().duration}
                  startAt={initialStart}
                  currentTime={currentTime()}
                  onTimeUpdate={setCurrentTime}
                  onReady={setAudio}
                  onError={setPlayerError}
                />
              </Show>
            </div>
            </div>

            <Show when={playerError()}>
              <div class="error-box" style={{ "margin-top": "0.75rem" }}>
                {playerError()}
              </div>
            </Show>

            <div class="set-body">
              <TrackList
                tracks={data().tracks}
                activeIndex={activeIndex()}
                onSeek={seek}
                onSelect={setActiveIndex}
                /* Hearing the set and the reference at once is the one thing
                   that makes this check useless, so starting one stops the
                   other. */
                onPreviewStart={() => audio()?.pause()}
              />
            </div>

            <Show when={data().source_url}>
              <div class="tiny faint" style={{ "margin-top": "1rem" }}>
                Source:{" "}
                <a href={data().source_url} target="_blank" rel="noopener noreferrer">
                  {data().source_url}
                </a>
                <Show when={data().uploader}> · {data().uploader}</Show>
              </div>
            </Show>

          </>
        )}
      </Show>
    </div>
  );
}
