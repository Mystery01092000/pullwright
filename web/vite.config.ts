import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8765', rewrite: (p) => p.replace(/^\/api/, '') },
      // GitHub login and chat-account linking are browser navigations, not fetches: forward them unchanged
      '/auth': 'http://localhost:8765',
      '/link': 'http://localhost:8765',
    },
  },
})
