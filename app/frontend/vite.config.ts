import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During development the FastAPI backend runs on :8000; the built app is served by FastAPI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: false },
      '/auth': { target: 'http://localhost:8000', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
