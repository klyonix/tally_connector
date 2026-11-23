import frappeUIPreset from 'frappe-ui/tailwind'

export default {
  presets: [frappeUIPreset],
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}',
    './node_modules/frappe-ui/src/**/*.{vue,js,ts,jsx,tsx}',
  ],
  theme: {
    extend: {
      colors: {
        // KlyONIX Tech. One accent ramp, not a palette: the neutrals stay
        // frappe-ui's so the app reads as part of the Desk rather than as a
        // visitor in it, and this is spent only on what is active, primary or
        // in progress.
        brand: {
          50: '#F1FAF8',
          100: '#DCF2ED',
          200: '#B6E4DA',
          300: '#84CFC1',
          400: '#4EB3A2',
          500: '#2A9585',
          600: '#1B786B',
          700: '#166057',
          800: '#144D46',
          900: '#12403B',
        },
      },
    },
  },
  plugins: [],
}
