import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve } from 'node:path'

const root = resolve(process.cwd(), '..')
const python = resolve(root, '.venv', 'Scripts', 'python.exe')
const args = process.argv.slice(2)
let api

try {
  await fetch('http://127.0.0.1:7101/api/bootstrap', { signal: AbortSignal.timeout(800) })
} catch {
  const executable = existsSync(python) ? python : 'python'
  api = spawn(executable, ['-m', 'support_bot.miniapp_server'], { cwd: root, stdio: 'inherit' })
}

const vite = spawn(process.execPath, [resolve('node_modules', 'vite', 'bin', 'vite.js'), ...args], { stdio: 'inherit' })
let closing = false
function close(code = 0) {
  if (closing) return
  closing = true
  if (api && !api.killed) api.kill('SIGTERM')
  if (!vite.killed) vite.kill('SIGTERM')
  setTimeout(() => process.exit(code), 150)
}
vite.on('exit', (code) => close(code ?? 0))
api?.on('exit', (code) => { if (code && !closing) close(code) })
process.on('SIGINT', () => close())
process.on('SIGTERM', () => close())
