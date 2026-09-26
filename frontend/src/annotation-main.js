import { createApp } from 'vue'
import AnnotationWorkbench from './components/AnnotationWorkbench.vue'
import './style.css'

createApp(AnnotationWorkbench, { apiBase: '', teamMode: true }).mount('#annotation-app')
