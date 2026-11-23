/**
 * The realtime half of job progress.
 *
 * Deliberately thin, and deliberately optional. Every field it carries is also
 * committed to the log and readable through `jobs.status`, so a site with no
 * socketio, a dropped connection or a browser that was closed all degrade to
 * polling rather than to a screen that says nothing. That is what makes the
 * poll a fallback instead of a second implementation of the same thing.
 */
import { io, type Socket } from 'socket.io-client'

let socket: Socket | null = null

/**
 * Frappe's socketio serves each site under its own namespace, on its own port
 * in development and behind the web server's port in production.
 */
function connect(): Socket | null {
  const w = window as any
  if (!w.site_name) return null
  const port = window.location.port ? `:${w.socketio_port || 9000}` : ''
  const protocol = port ? 'http' : 'https'
  const url = `${protocol}://${window.location.hostname}${port}/${w.site_name}`
  return io(url, { withCredentials: true, reconnectionAttempts: 5 })
}

export function getSocket() {
  if (socket) return socket
  try {
    socket = connect()
  } catch (e) {
    // A dashboard that cannot open a socket must still export. The store polls.
    console.warn('[tally-connector] realtime unavailable, falling back to polling', e)
    socket = null
  }
  return socket
}

/**
 * Watch one log document.
 *
 * Frappe's socketio puts a client in `doc:<doctype>/<name>` only after a
 * `doc_subscribe`, and only if that client may read the document - so this is
 * fenced by the same permissions the REST call is, rather than by us.
 */
export function subscribeToDoc(doctype: string, name: string) {
  const s = getSocket()
  if (!s) return
  s.emit('doc_subscribe', doctype, name)
}

export function unsubscribeFromDoc(doctype: string, name: string) {
  const s = getSocket()
  if (!s) return
  s.emit('doc_unsubscribe', doctype, name)
}

export function onEvent(event: string, handler: (data: any) => void) {
  const s = getSocket()
  if (!s) return () => {}
  s.on(event, handler)
  return () => s.off(event, handler)
}
