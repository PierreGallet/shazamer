// @ts-check
/** Documentation site for Shazamer.
 *
 * Kept in its own directory rather than merged into the app's `web/` project:
 * they have separate dependency trees, separate build outputs, and no reason
 * to be rebuilt together.
 */

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: "Shazamer",
  tagline: "Identify every track in a DJ set, then go find the records",
  favicon: "img/favicon.svg",

  // Served by the app itself, under /docs — not on GitHub Pages. The app
  // mounts docs-site/build there, so baseUrl has to match that prefix or
  // every asset URL the build emits points somewhere that does not exist.
  url: "https://shazamer.pierregallet.com",
  baseUrl: "/docs/",
  organizationName: "PierreGallet",
  projectName: "shazamer",

  onBrokenLinks: "throw",
  onBrokenMarkdownLinks: "warn",

  i18n: { defaultLocale: "en", locales: ["en"] },

  presets: [
    [
      "classic",
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          sidebarPath: "./sidebars.js",
          routeBasePath: "/",
          editUrl: "https://github.com/PierreGallet/shazamer/tree/main/docs-site/",
        },
        blog: false,
        theme: { customCss: "./src/css/custom.css" },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      colorMode: { defaultMode: "dark", respectPrefersColorScheme: true },
      navbar: {
        title: "Shazamer",
        items: [
          { type: "docSidebar", sidebarId: "docs", position: "left", label: "Docs" },
          // Back to the app. Raw HTML because every link type the navbar
          // offers runs the URL through baseUrl: both `to: "/"` and
          // `href: "/"` come out as "/docs/", pointing at this page.
          {
            type: "html",
            position: "right",
            value: '<a class="navbar__item navbar__link" href="/">\u2190 App</a>',
          },
          {
            href: "https://github.com/PierreGallet/shazamer",
            label: "GitHub",
            position: "right",
          },
        ],
      },
      footer: {
        style: "dark",
        copyright: `MIT licensed. Built for digging.`,
      },
      prism: { additionalLanguages: ["bash", "python", "yaml", "json"] },
    }),
};

export default config;
