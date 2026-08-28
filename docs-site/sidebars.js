/** Sidebar layout.
 *
 * Ordered the way someone actually meets the project: what it does, then how
 * to run it, then how it works inside, then the decisions that shaped it.
 */
const sidebars = {
  docs: [
    "intro",
    {
      type: "category",
      label: "Using it",
      collapsed: false,
      items: ["running", "analysing", "digging", "acquiring"],
    },
    {
      type: "category",
      label: "How it works",
      collapsed: false,
      items: ["architecture", "pipeline", "identification", "deployment"],
    },
    {
      type: "category",
      label: "Decisions",
      items: ["memory", "lessons"],
    },
  ],
};

export default sidebars;
