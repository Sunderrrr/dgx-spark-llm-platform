// Light/dark preference, mirrored in a cookie so the server renders the page in
// it (app/layout.tsx). Outside the "use client" theme provider: a constant
// exported from one is only a reference on the server side.
export const MODE_COOKIE = "dgx_theme_mode";
