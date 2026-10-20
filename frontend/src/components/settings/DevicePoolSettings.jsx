/**
 * Per-device pool configuration.
 *
 * Two independent things per device:
 *  - Mining target: written to the miner itself over the cgminer API (setpool).
 *  - Pool statistics: which pool API MinerSentinel polls for this device's
 *    payout/share stats. Stored in the device's connection_config.
 */
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import api from '@/lib/api'
import { AlertTriangle, Check, Copy, Loader2, Radio } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

const POOL_STATS_TYPES = [
  { value: '', label: 'None' },
  { value: 'ckpool', label: 'CKPool (solo)' },
  { value: 'publicpool', label: 'Public Pool' },
  { value: 'f2pool', label: 'f2pool (API token)' },
]

function emptyForm() {
  return {
    pool_url: '',
    worker: '',
    worker_password: 'x',
    miner_user: 'root',
    miner_password: '',
    reboot: true,
    stats_type: '',
    stats_address: '',
    stats_url: '',
    stats_token: '',
    stats_worker: '',
  }
}

function formFromDevice(device, pools) {
  const stats = device.connection_config?.pool_stats || {}
  const active = pools?.find((pool) => pool.stratum_active) || pools?.[0]
  return {
    ...emptyForm(),
    pool_url: active?.url || '',
    worker: active?.user || '',
    stats_type: stats.type || '',
    stats_address: stats.address || '',
    stats_url: stats.url || '',
    stats_token: stats.token || '',
    stats_worker: stats.worker || '',
  }
}

function DeviceCard({ device, devices, forms, setForm, pools, onReloadPools }) {
  const form = forms[device.device_id] || emptyForm()
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState(null)
  const devicePools = pools[device.device_id]

  const update = (patch) => setForm(device.device_id, { ...form, ...patch })

  const copyFrom = (sourceId) => {
    const source = forms[sourceId]
    if (!source) return
    update({
      pool_url: source.pool_url,
      worker: source.worker,
      worker_password: source.worker_password,
      stats_type: source.stats_type,
      stats_address: source.stats_address,
      stats_url: source.stats_url,
      stats_token: source.stats_token,
    })
    setMessage({ tone: 'info', text: 'Copied from ' + sourceId + ' - review, then apply.' })
  }

  const applyMiningTarget = async () => {
    setBusy(true)
    setMessage(null)
    try {
      const response = await api.post('/api/devices/' + device.id + '/set-pool/', {
        pool_url: form.pool_url,
        worker: form.worker,
        worker_password: form.worker_password,
        miner_user: form.miner_user,
        miner_password: form.miner_password,
        reboot: form.reboot,
      })
      setMessage({
        tone: 'success',
        text: response.data?.rebooted
          ? 'Sent to the miner and rebooted - it comes back on the new pool in a minute.'
          : 'Sent to the miner. It applies the pool after the next reboot.',
      })
      setTimeout(() => onReloadPools(device), 20000)
    } catch (err) {
      setMessage({
        tone: 'error',
        text: err.response?.data?.error || 'Could not write the pool to this device.',
      })
    } finally {
      setBusy(false)
    }
  }

  const saveStats = async () => {
    setBusy(true)
    setMessage(null)
    try {
      const connectionConfig = {
        ...(device.connection_config || {}),
        pool_stats: form.stats_type
          ? {
              type: form.stats_type,
              address: form.stats_address,
              url: form.stats_url,
              token: form.stats_token,
              worker: form.stats_worker,
            }
          : undefined,
      }
      if (!form.stats_type) delete connectionConfig.pool_stats
      await api.patch('/api/devices/' + device.id + '/', { connection_config: connectionConfig })
      setMessage({ tone: 'success', text: 'Pool statistics settings saved for this device.' })
    } catch (err) {
      setMessage({
        tone: 'error',
        text: err.response?.data?.detail || 'Could not save the statistics settings.',
      })
    } finally {
      setBusy(false)
    }
  }

  const others = devices.filter((entry) => entry.device_id !== device.device_id)

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle className="text-base font-semibold">
              {device.device_name || device.device_id}
            </CardTitle>
            <CardDescription className="text-xs">
              {device.model || device.make} · {device.ip_address}
            </CardDescription>
          </div>
          {others.length > 0 && (
            <div className="flex items-center gap-2">
              <Copy className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
              <select
                className="h-8 rounded-md border border-input bg-background px-2 text-xs"
                value=""
                onChange={(event) => {
                  if (event.target.value) copyFrom(event.target.value)
                  event.target.value = ''
                }}
              >
                <option value="">Copy from…</option>
                {others.map((entry) => (
                  <option key={entry.device_id} value={entry.device_id}>
                    {entry.device_name || entry.device_id}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-5">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Radio className="h-4 w-4 text-muted-foreground" aria-hidden />
            <p className="text-sm font-medium">Currently mining to</p>
          </div>
          {devicePools === undefined && (
            <p className="text-xs text-muted-foreground">Reading from device…</p>
          )}
          {devicePools?.length === 0 && (
            <p className="text-xs text-muted-foreground">No pools reported.</p>
          )}
          {devicePools?.map((pool) => (
            <div
              key={pool.index}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-muted/40 px-3 py-2 text-xs"
            >
              <span className="font-medium">{pool.url}</span>
              <span className="text-muted-foreground">{pool.user}</span>
              <Badge variant={pool.stratum_active ? 'secondary' : 'outline'}>
                {pool.stratum_active ? 'active' : pool.status}
              </Badge>
            </div>
          ))}
        </div>

        <div className="space-y-3 border-t border-border/60 pt-4">
          <p className="text-sm font-medium">Mining target (written to the miner)</p>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label className="text-xs">Pool URL</Label>
              <Input
                value={form.pool_url}
                onChange={(event) => update({ pool_url: event.target.value })}
                placeholder="stratum+tcp://public-pool.io:21496"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Worker</Label>
              <Input
                value={form.worker}
                onChange={(event) => update({ worker: event.target.value })}
                placeholder="bc1... or account.worker"
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Worker password</Label>
              <Input
                value={form.worker_password}
                onChange={(event) => update({ worker_password: event.target.value })}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Miner login (user / password)</Label>
              <div className="flex gap-2">
                <Input
                  value={form.miner_user}
                  onChange={(event) => update({ miner_user: event.target.value })}
                  placeholder="root"
                />
                <Input
                  type="password"
                  value={form.miner_password}
                  onChange={(event) => update({ miner_password: event.target.value })}
                  placeholder="miner password"
                />
              </div>
            </div>
          </div>
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={form.reboot}
              onChange={(event) => update({ reboot: event.target.checked })}
            />
            Reboot the miner right away (Avalon applies pool changes only after a reboot)
          </label>
          <Button size="sm" disabled={busy} onClick={applyMiningTarget}>
            {busy ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : null}
            Apply to this device
          </Button>
          <p className="text-[11px] text-muted-foreground">
            The miner login is only used for this request and is never stored.
          </p>
        </div>

        <div className="space-y-3 border-t border-border/60 pt-4">
          <p className="text-sm font-medium">Pool statistics (what MinerSentinel polls)</p>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <div className="space-y-1.5">
              <Label className="text-xs">Source</Label>
              <select
                className="h-9 w-full rounded-md border border-input bg-background px-2 text-sm"
                value={form.stats_type}
                onChange={(event) => update({ stats_type: event.target.value })}
              >
                {POOL_STATS_TYPES.map((entry) => (
                  <option key={entry.value} value={entry.value}>
                    {entry.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">
                {form.stats_type === 'f2pool' ? 'Account' : 'Bitcoin address'}
              </Label>
              <Input
                value={form.stats_address}
                onChange={(event) => update({ stats_address: event.target.value })}
                placeholder={form.stats_type === 'f2pool' ? 'f2pool account name' : 'bc1...'}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">
                {form.stats_type === 'f2pool' ? 'API token' : 'Pool URL (optional)'}
              </Label>
              {form.stats_type === 'f2pool' ? (
                <Input
                  type="password"
                  value={form.stats_token}
                  onChange={(event) => update({ stats_token: event.target.value })}
                  placeholder="F2P-API-SECRET"
                />
              ) : (
                <Input
                  value={form.stats_url}
                  onChange={(event) => update({ stats_url: event.target.value })}
                  placeholder="https://eusolo.ckpool.org"
                />
              )}
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Worker name</Label>
              <Input
                value={form.stats_worker}
                onChange={(event) => update({ stats_worker: event.target.value })}
                placeholder="001"
              />
            </div>
          </div>
          <p className="text-[11px] text-muted-foreground">
            The worker name is how pool-side numbers are matched to this device - use the part
            after the dot in the mining target above.
          </p>
          {form.stats_type === 'f2pool' && (
            <p className="text-[11px] text-muted-foreground">
              f2pool has no public statistics endpoint - the account API token is required and is
              stored with this device.
            </p>
          )}
          <Button size="sm" variant="secondary" disabled={busy} onClick={saveStats}>
            {busy ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : null}
            Save statistics settings
          </Button>
        </div>

        {message && (
          <p
            className={
              'flex items-start gap-2 text-xs ' +
              (message.tone === 'error'
                ? 'text-status-critical-fg'
                : message.tone === 'success'
                  ? 'text-status-online-fg'
                  : 'text-muted-foreground')
            }
          >
            {message.tone === 'error' ? (
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            ) : (
              <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            )}
            {message.text}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

export default function DevicePoolSettings() {
  const [devices, setDevices] = useState([])
  const [pools, setPools] = useState({})
  const [forms, setForms] = useState({})
  const [loading, setLoading] = useState(true)

  const loadPools = useCallback(async (device) => {
    try {
      const response = await api.get('/api/devices/' + device.id + '/pools/')
      const list = response.data?.pools || []
      setPools((current) => ({ ...current, [device.device_id]: list }))
      return list
    } catch {
      setPools((current) => ({ ...current, [device.device_id]: [] }))
      return []
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const response = await api.get('/api/devices/')
        const list = Array.isArray(response.data) ? response.data : response.data?.results || []
        if (cancelled) return
        setDevices(list)
        const poolLists = await Promise.all(list.map((device) => loadPools(device)))
        if (cancelled) return
        const nextForms = {}
        list.forEach((device, index) => {
          nextForms[device.device_id] = formFromDevice(device, poolLists[index])
        })
        setForms(nextForms)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [loadPools])

  const setForm = (deviceId, value) => setForms((current) => ({ ...current, [deviceId]: value }))

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading devices…</p>
  }

  if (devices.length === 0) {
    return <p className="text-sm text-muted-foreground">No devices configured yet.</p>
  }

  return (
    <div className="space-y-4">
      {devices.map((device) => (
        <DeviceCard
          key={device.device_id}
          device={device}
          devices={devices}
          forms={forms}
          setForm={setForm}
          pools={pools}
          onReloadPools={loadPools}
        />
      ))}
    </div>
  )
}
