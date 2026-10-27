/**
 * The share glyph: a person and a plus.
 *
 * Sharing here goes from one account to another, so the mark is a person —
 * the convention Google Docs and Figma use for the same act. The iOS "box with
 * an arrow up" was the other candidate and is ruled out by this app's own
 * vocabulary: a tray with an arrow *down* already means "where to get it"
 * (Crate), and two icons told apart only by an arrow's direction are one
 * icon read twice.
 *
 * Drawn like every other inline icon here: 24 grid, current colour, 2 px
 * round strokes. One component so the button, the "shared by" chip and the
 * inbox all show the same mark.
 */
export function ShareIcon(props: { size?: number }) {
  const size = props.size ?? 15;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none"
         stroke="currentColor" stroke-width="2"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5" />
      <path d="M19 8v6M16 11h6" />
    </svg>
  );
}
