/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      // Several studio empty states use these compact card sizes. Keep the
      // utilities explicit so the image boxes reserve their intended space
      // before remote thumbnails finish loading.
      width: {
        18: "4.5rem",
      },
      height: {
        18: "4.5rem",
        22: "5.5rem",
      },
      fontFamily: {
        sans: ["Inter", "var(--font-inter)", "-apple-system", "BlinkMacSystemFont", "Segoe UI", "Roboto", "sans-serif"],
      },
      colors: {
        'app-bg': '#050505',
        'panel-bg': '#0a0a0a',
        'card-bg': '#111111',
        primary: '#22d3ee',
        secondary: '#a1a1aa',
        muted: '#52525b',
      },
    },
  },
  plugins: [],
}
