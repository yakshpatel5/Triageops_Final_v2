import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/ops':      { target: 'http://localhost:8000', changeOrigin: true },
      '/webhook':  { target: 'http://localhost:8000', changeOrigin: true },
      '/slack':    { target: 'http://localhost:8000', changeOrigin: true },
      '/health':   { target: 'http://localhost:8000', changeOrigin: true },
      '/dashboard':{ target: 'http://localhost:8000', changeOrigin: true },
    }
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  }
})
