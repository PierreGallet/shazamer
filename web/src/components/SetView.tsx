import { For, Show, createEffect, createResource, createSignal } from "solid-js";
import { api, formatDuration, formatTime } from "../lib/api";
import Player from "./Player";
import TrackList from "./TrackList";
import Waveform from "./Waveform";

/** One analysed set: waveform, transport, tracklist and exports. */

interface Props {
  setId: string;
  onBack: () => void;
}

const EXPORTS: { fmt: string; label: string; hint: string }[] = [
  { fmt: "rekordbox", label: "Rekordbox XML", hint: "Playlist with BPM and key filled in" },
  { fmt: "txt", label: "Text", hint: "The classic tracklist" },
  { fmt: "csv", label: "CSV", hint: "Spreadsheet with every field" },
  { fmt: "m3u", label: "M3U", hint: "Playlist file" },
  { fmt: "json", label: "JSON", hint: "Everything, raw" },
];

export default function SetView(props: Props) {
  const [detail] = createResource(() => props.setId, api.set);
  const [currentTime, setCurrentTime] = createSignal(0);
  const [activeIndex, setActiveIndex] = createSignal<number | null>(null);
  const [playerError, setPlayerError] = createSignal("");
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
                    <span>analysed in {formatTime(data().stats.elapsed_seconds!)}</span>
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
                    Audio for this set has been cleared. The tracklist and waveform
                    remain — re-run the source URL to play it again.
                  </div>
                }
              >
                <Player
                  src={api.audioUrl(props.setId)}
                  duration={data().duration}
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
