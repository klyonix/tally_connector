import { createApp } from 'vue'
import { Button, FormControl, frappeRequest, resourcesPlugin, setConfig } from 'frappe-ui'

import App from './App.vue'
import router from './router'
import './index.css'

const app = createApp(App)
setConfig('resourceFetcher', frappeRequest)

app.use(router)
app.use(resourcesPlugin)

app.component('Button', Button)
app.component('FormControl', FormControl)
app.mount('#app')
