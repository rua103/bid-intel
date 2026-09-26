import { createApp, h } from 'vue'
import AuthGate from './components/AuthGate.vue'
import AnnotationWorkbench from './components/AnnotationWorkbench.vue'
import { resolveApiBase } from './utils/browser.js'
import './style.css'

const apiBase = resolveApiBase(import.meta.env.VITE_API_BASE, window.location, import.meta.env.VITE_API_PORT || '8000')

createApp({
  render() {
    return h(AuthGate, { apiBase }, {
      default: () => h(AnnotationWorkbench, { apiBase, teamMode: true }),
    })
  },
}).mount('#annotation-app')
