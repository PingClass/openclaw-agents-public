<script setup lang="ts">
// 通知收件匣（計畫 0.5）：每位確認者一個收件匣，LINE 接上前以這裡的確認連結驗收。
// admin 可以切換查看任一人的收件匣；其他帳號看自己對應的確認者。
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { fetchInbox, fetchRecipients, markAllRead, markRead, setConfirmMode } from '../api'
import { INBOX_APPROVAL_LABELS, formatTime, label, localApprovalPath, maskText } from '../format'
import { useLiveUpdates } from '../live'
import { errorMessage, useResource } from '../resource'
import { isAdmin, session } from '../session'
import type { ConfirmMode, NotificationItem, Recipient } from '../types'

const route = useRoute()
const router = useRouter()
const recipients = ref<Recipient[]>([])
const unreadOnly = ref(false)
const actionError = ref('')

const selected = computed(() => (typeof route.query.recipient === 'string' ? route.query.recipient : ''))

const inbox = useResource(() => fetchInbox(isAdmin.value ? selected.value || undefined : undefined, unreadOnly.value))
const notMapped = computed(() => inbox.code.value === 'recipient_not_found' && !isAdmin.value)

async function loadRecipients() {
  if (!isAdmin.value) return
  try {
    recipients.value = (await fetchRecipients()).items
  } catch {
    recipients.value = []
  }
  // admin 沒指定收件人、自己也沒對應確認者時，預設看第一位
  if (!selected.value && !session.me?.identity_id && recipients.value.length) {
    await router.replace({ name: 'inbox', query: { recipient: recipients.value[0].hrm_employee_id } })
  }
}

function refresh() {
  void inbox.reload()
  void loadRecipients()
}

onMounted(async () => {
  await loadRecipients()
  void inbox.reload()
})
watch([selected, unreadOnly], () => void inbox.reload())
useLiveUpdates(refresh, {
  filter: (event) => event.type === 'notification.created' || event.type.startsWith('approval.'),
})

function pick(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  void router.push({ name: 'inbox', query: value ? { recipient: value } : {} })
}

const canWrite = computed(() => {
  const recipient = inbox.data.value?.recipient
  return Boolean(isAdmin.value || (recipient && session.me?.identity_id === recipient.id))
})

async function read(item: NotificationItem) {
  actionError.value = ''
  try {
    await markRead(item.id)
    refresh()
  } catch (err) {
    actionError.value = errorMessage(err)
  }
}

async function readAll() {
  actionError.value = ''
  try {
    await markAllRead(isAdmin.value ? inbox.data.value?.recipient.hrm_employee_id : undefined)
    refresh()
  } catch (err) {
    actionError.value = errorMessage(err)
  }
}

// 確認方式：agent 代理／真人（計畫 9.4，課堂 demo 由講師親自確認）。只有 admin 能切換。
const confirmMode = computed<ConfirmMode | null>(() => inbox.data.value?.recipient.confirm_mode ?? null)
const switching = ref(false)
// 要切換成哪一種（切換前先二次確認，和流程操作一樣）
const pendingMode = ref<ConfirmMode | null>(null)
const recipientName = computed(() => {
  const recipient = inbox.data.value?.recipient
  return recipient ? recipient.display_name || recipient.hrm_employee_id : ''
})
const modePrompt = computed(() =>
  pendingMode.value === 'human'
    ? {
        title: `把 ${recipientName.value} 切成「真人確認」？`,
        body: '切換後扮演他的員工 agent 不會再代為確認（也拿不到確認連結），要由你在這裡打開確認頁。會寫入稽核。',
      }
    : {
        title: `把 ${recipientName.value} 切回「agent 代理」？`,
        body: '還沒決定的確認會交回員工 agent，下次檢查收件匣時處理。會寫入稽核。',
      },
)
// 換收件人時收起確認框
watch(selected, () => (pendingMode.value = null))

async function switchConfirmMode() {
  const recipient = inbox.data.value?.recipient
  const mode = pendingMode.value
  if (!recipient || !mode || switching.value) return
  actionError.value = ''
  switching.value = true
  try {
    await setConfirmMode(recipient.hrm_employee_id, mode)
    pendingMode.value = null
    refresh()
  } catch (err) {
    actionError.value = errorMessage(err)
  } finally {
    switching.value = false
  }
}

/** 確認還能做嗎：舊版後端沒有 approval_status 時視為可以 */
function approvalOpen(item: NotificationItem): boolean {
  return !item.approval_status || item.approval_status === 'pending'
}

function approvalStatusText(item: NotificationItem): string {
  const text = label(INBOX_APPROVAL_LABELS, item.approval_status)
  return item.approval_status === 'decided' && item.approval_decision ? `${text}：${item.approval_decision}` : text
}

const emptyText = computed(() => {
  if (inbox.error.value) return ''
  return unreadOnly.value ? '沒有未讀通知' : '收件匣是空的'
})
</script>

<template>
  <div class="page">
    <h1>通知收件匣</h1>
    <div class="panel inbox-bar">
      <label v-if="isAdmin" class="recipient-picker">收件人
        <select :value="selected || inbox.data.value?.recipient.hrm_employee_id || ''" name="recipient" @change="pick">
          <option v-if="session.me?.identity_id" value="">我自己</option>
          <option v-for="person in recipients" :key="person.id" :value="person.hrm_employee_id">
            {{ person.display_name || person.hrm_employee_id }}<template v-if="person.employee_number">（{{ person.employee_number }}）</template><template v-if="person.confirm_mode === 'human'">・真人確認</template><template v-if="person.unread_count">・未讀 {{ person.unread_count }}</template>
          </option>
        </select>
      </label>
      <label class="check"><input v-model="unreadOnly" type="checkbox" name="unread" />只看未讀</label>
      <button v-if="canWrite && inbox.data.value?.unread_count" type="button" class="ghost" @click="readAll">全部標為已讀</button>
    </div>

    <p v-if="notMapped" class="muted" data-testid="no-inbox">
      這個帳號沒有對應的確認者，所以沒有收件匣。請 admin 用 <code>create-user --identity</code> 綁定。
    </p>
    <p v-else-if="inbox.error.value" role="alert" class="error">{{ inbox.error.value }}</p>
    <p v-if="actionError" role="alert" class="error">{{ actionError }}</p>

    <section v-if="isAdmin && inbox.data.value && confirmMode" class="panel confirm-mode-bar" data-testid="confirm-mode">
      <span>
        確認方式：
        <strong v-if="confirmMode === 'human'" class="tag human">真人確認</strong>
        <strong v-else class="tag agent">agent 代理</strong>
      </span>
      <span class="muted small">
        {{ confirmMode === 'human'
          ? '員工 agent 不會代為確認，請在下方打開確認頁。'
          : '扮演這個人的員工 agent 會定時檢查收件匣並代為確認。' }}
      </span>
      <button
        v-if="confirmMode === 'human'"
        type="button"
        class="ghost"
        :disabled="switching"
        data-testid="confirm-mode-agent"
        @click="pendingMode = 'agent'"
      >改回 agent 代理</button>
      <button
        v-else
        type="button"
        :disabled="switching"
        data-testid="confirm-mode-human"
        @click="pendingMode = 'human'"
      >改成真人確認</button>
      <div v-if="pendingMode" class="confirm-box" role="alertdialog" aria-modal="false" :aria-label="modePrompt.title" data-testid="confirm-mode-box">
        <strong>{{ modePrompt.title }}</strong>
        <p class="small">{{ modePrompt.body }}</p>
        <div class="action-row">
          <button type="button" data-testid="confirm-mode-yes" :disabled="switching" @click="switchConfirmMode">
            {{ switching ? '處理中…' : '確定切換' }}
          </button>
          <button type="button" class="ghost" data-testid="confirm-mode-no" :disabled="switching" @click="pendingMode = null">先不要</button>
        </div>
      </div>
    </section>

    <template v-if="inbox.data.value">
      <p class="muted small">
        {{ inbox.data.value.recipient.display_name || inbox.data.value.recipient.hrm_employee_id }} 的收件匣・未讀 {{ inbox.data.value.unread_count }}
      </p>
      <p v-if="!inbox.data.value.items.length" class="muted">{{ emptyText }}</p>
      <ul class="inbox-list">
        <li
          v-for="item in inbox.data.value.items"
          :key="item.id"
          class="inbox-item"
          :class="{ unread: !item.read_at, closed: !approvalOpen(item) }"
          :data-id="item.id"
        >
          <div class="inbox-main">
            <strong>{{ item.title }}</strong>
            <span
              v-if="item.approval_status"
              class="badge"
              :class="'st-' + item.approval_status"
              data-testid="approval-status"
            >{{ approvalStatusText(item) }}</span>
            <p v-if="item.body" class="small pre">{{ maskText(item.body) }}</p>
            <p class="small muted">
              {{ formatTime(item.created_at) }}<template v-if="item.read_at">・已讀</template>
              <template v-if="item.flow_id">・<router-link :to="`/flows/${item.flow_id}`">查看流程</router-link></template>
            </p>
          </div>
          <div class="inbox-actions">
            <template v-if="approvalOpen(item)">
              <router-link v-if="localApprovalPath(item.link)" :to="localApprovalPath(item.link)!" class="button" data-testid="approval-link">
                開啟確認頁
              </router-link>
              <span v-else-if="item.link_hidden" class="muted small">確認連結只給本人與 admin</span>
            </template>
            <button v-if="canWrite && !item.read_at" type="button" class="ghost" @click="read(item)">標為已讀</button>
          </div>
        </li>
      </ul>
    </template>
  </div>
</template>
