import { Router, Route, useNavigate, useParams, useLocation, A } from "@solidjs/router";
import { For, Show, createResource, createSignal, onCleanup, type ParentProps } from "solid-js";
import Analyze from "./components/Analyze";
import Crate from "./components/Crate";
import Library from "./components/Library";
import SetView from "./components/SetView";
import Watches from "./components/Watches";
import { api } from "./lib/api";

/**
 * Routes, so the app has addresses.
 *
 * Everything worth returning to has a URL: a set you found, an analysis you
 * kicked off, the crate you were filtering. Without that, sharing "listen to
 * this" means describing where to click, refreshing during a twenty-minute
 * analysis loses the progress view, and the browser's back button does
 * nothing — which reads as broken rather than minimal.
 */

const NAV: { href: string; label: string }[] = [
  { href: "/", label: "Analyse" },
  { href: "/library", label: "Library" },
  { href: "/crate", label: "Crate" },
  { href: "/following", label: "Following" },
];

function Shell(props: ParentProps) {
  const [sets] = createResource(() => api.sets(60));
  const location = useLocation();

  /**
   * Running analyses, surfaced everywhere.
   *
   * A set takes minutes to analyse and people do not sit and watch it — they
   * go look at the library, or open another tab. Without a global marker the
   * only way back to a run in progress is remembering its URL, so the analysis
   * feels lost even though it is still going.
   *
   * Polled rather than streamed: this is one small list for the header, and a
   * second SSE connection alongside the per-task one buys nothing.
   */
  const [tick, setTick] = createSignal(0);
  const timer = setInterval(() => setTick((n) => n + 1), 4000);
  onCleanup(() => clearInterval(timer));
  const [active] = createResource(tick, () => api.activeTasks());

  // Keep the last good value while a poll is in flight, so the pill does not
  // blink out and back on every refresh.
  const running = () => active.latest ?? [];
  const onItsPage = () => location.pathname.startsWith("/analyzing/");

  // A set is reached from the library and belongs to it, but /sets/:id is not
  // a child path, so activeClass alone would unlight the tab you came from.
  const activeHref = () => {
    const path = location.pathname;
    if (path.startsWith("/sets/")) return "/library";
    if (path.startsWith("/analyzing/")) return "/";
    return path;
  };

  return (
    <>
      <header class="app-header">
        <A href="/" class="brand" aria-label="Shazamer home">
          <span class="brand-mark">
            <svg viewBox="0 0 24 24" width="15" height="15" fill="#fff">
              <rect x="3" y="10" width="2.4" height="4" rx="1.2" />
              <rect x="8" y="6.5" width="2.4" height="11" rx="1.2" />
              <rect x="13" y="4" width="2.4" height="16" rx="1.2" />
              <rect x="18" y="8.5" width="2.4" height="7" rx="1.2" />
            </svg>
          </span>
          <span class="brand-name">Shazamer</span>
        </A>

        <nav class="nav">
          {NAV.map((item) => (
            <A
              href={item.href}
              class="nav-item"
              classList={{ active: activeHref() === item.href }}
            >
              {item.label}
              <Show when={item.href === "/library" && sets()?.length}>
                <span class="nav-count">{sets()!.length}</span>
              </Show>
            </A>
          ))}
        </nav>

        {/* /docs is served by the API from the Docusaurus build, not by this
            app. rel="external" is what stops the client router taking the
            click — it intercepts every same-origin anchor otherwise, and a
            plain <a> lands on the app's own "Nothing here". */}
        <a class="nav-item nav-docs" href="/docs/" rel="external">Docs</a>

        <Show when={running().length > 0 && !onItsPage()}>
          <For each={running().slice(0, 2)}>
            {(task) => (
              <A href={`/analyzing/${task.task_id}`} class="running-pill"
                 title={task.message}>
                <span class="running-dot" />
                <span class="running-label">
                  {task.filename && task.filename !== "Resolving..."
                    ? task.filename
                    : "Analysing"}
                </span>
                <span class="running-pct mono">{task.progress}%</span>
              </A>
            )}
          </For>
          <Show when={running().length > 2}>
            <span class="tiny faint">+{running().length - 2}</span>
          </Show>
        </Show>
      </header>

      <main>{props.children}</main>
    </>
  );
}

function AnalyzeRoute() {
  const navigate = useNavigate();
  const params = useParams<{ taskId?: string }>();
  return (
    <Analyze
      // Keyed on the task id so navigating to a different analysis remounts
      // rather than leaving the previous one's stream attached.
      taskId={params.taskId}
      onStarted={(taskId) => navigate(`/analyzing/${taskId}`, { replace: true })}
      onComplete={(setId) => navigate(`/sets/${setId}`, { replace: true })}
    />
  );
}

function LibraryRoute() {
  const navigate = useNavigate();
  return <Library onOpen={(id) => navigate(`/sets/${id}`)} />;
}

function SetRoute() {
  const params = useParams<{ id: string }>();
  const navigate = useNavigate();
  return (
    <SetView
      setId={params.id}
      onBack={() => navigate("/library")}
      onReanalyse={(taskId) => navigate(`/analyzing/${taskId}`)}
    />
  );
}

function NotFound() {
  return (
    <div class="wrap">
      <div class="empty">
        <div class="empty-title">Nothing here</div>
        <div class="small">
          That address does not match anything. <A href="/">Start an analysis</A>{" "}
          or <A href="/library">open the library</A>.
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <Router root={Shell}>
      <Route path="/" component={AnalyzeRoute} />
      {/* A running analysis is addressable, so a refresh resumes watching it
          instead of dropping you back to an empty form. */}
      <Route path="/analyzing/:taskId" component={AnalyzeRoute} />
      <Route path="/library" component={LibraryRoute} />
      <Route path="/sets/:id" component={SetRoute} />
      <Route path="/crate" component={Crate} />
      <Route path="/following" component={Watches} />
      <Route path="*" component={NotFound} />
    </Router>
  );
}
