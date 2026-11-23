import vue from '@vitejs/plugin-vue'
import frappeui from 'frappe-ui/vite'
import path from 'path'
import { defineConfig } from 'vite'

export default defineConfig({
	define: {
		__VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
	},
	plugins: [
		vue(),
		frappeui({
			frappeProxy: true,
			lucideIcons: true,
			jinjaBootData: true,
			buildConfig: {
				outDir: path.resolve(import.meta.dirname, `../${getAppName()}/public/frontend`),
				indexHtmlPath: path.resolve(
					import.meta.dirname,
					`../${getAppName()}/www/${getAppName()}.html`
				),
			},
		}),
	],
	build: {
		outDir: path.resolve(import.meta.dirname, `../${getAppName()}/public/frontend`),
		emptyOutDir: true,
	},
	server: {
		allowedHosts: true,
	},
	resolve: {
		alias: {
			'@': path.resolve(import.meta.dirname, 'src'),
			'tailwind.config.js': path.resolve(import.meta.dirname, 'tailwind.config.js'),
		},
	},
	optimizeDeps: {
		include: ['tailwind.config.js'],
	},
})

function getAppName() {
	// frappe-ui projects are structured as follows:
	// - apps
	//   - <app_name>
	//     - frontend
	//       - vite.config.js
	return path.basename(path.resolve(import.meta.dirname, '..'))
}
