const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000/ingest';
const BACKEND_REACTIONS_URL = process.env.BACKEND_REACTIONS_URL || 'http://127.0.0.1:8000/reactions/ingest';
const BACKEND_COMMAND_NEXT_URL = process.env.BACKEND_COMMAND_NEXT_URL || 'http://127.0.0.1:8000/bot/commands/next';
const BACKEND_COMMAND_RESULT_URL = process.env.BACKEND_COMMAND_RESULT_URL || 'http://127.0.0.1:8000/bot/commands';
const COMMAND_POLL_INTERVAL_MS = Number.parseInt(process.env.COMMAND_POLL_INTERVAL_MS || '3000', 10);
const WA_CLIENT_ID = process.env.WA_CLIENT_ID || 'wa-data-bot';
const MAX_INIT_RETRIES = Number.parseInt(process.env.WA_INIT_RETRIES || '5', 10);

const NODE_MAJOR = Number.parseInt(process.versions.node.split('.')[0], 10);
if (NODE_MAJOR >= 25) {
  console.warn('Node 25 is non-LTS and can be unstable with whatsapp-web.js. Prefer Node 24/22 LTS.');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isExecutionContextDestroyed(error) {
  const message = error instanceof Error ? error.message : String(error || '');
  return message.includes('Execution context was destroyed');
}

function normalizeId(value) {
  if (!value) {
    return null;
  }
  if (typeof value === 'string') {
    return value;
  }
  if (value._serialized) {
    return value._serialized;
  }
  if (value.id && value.id._serialized) {
    return value.id._serialized;
  }
  if (value.id) {
    return String(value.id);
  }
  return String(value);
}

function getMessageSerializedId(msg) {
  if (!msg) {
    return null;
  }
  if (msg.id && msg.id._serialized) {
    return msg.id._serialized;
  }
  return normalizeId(msg.id || msg._serialized || null);
}

async function reportCommandResult(commandId, payload) {
  const url = `${BACKEND_COMMAND_RESULT_URL}/${commandId}/result`;
  await axios.post(url, payload);
}

async function pollAndExecuteOutboundCommands() {
  try {
    const response = await axios.get(BACKEND_COMMAND_NEXT_URL);
    const data = response.data || {};

    if (data.status !== 'ok' || !data.command) {
      return;
    }

    const command = data.command;
    const commandId = command.id;

    try {
      const result = await client.sendMessage(command.target_group_id, command.text);
      await reportCommandResult(commandId, {
        status: 'sent',
        wa_message_id: getMessageSerializedId(result),
        sent_at: Math.floor(Date.now() / 1000),
      });
      console.log('Outgoing command sent successfully:', commandId);
    } catch (error) {
      await reportCommandResult(commandId, {
        status: 'failed',
        error_message: error instanceof Error ? error.message : String(error),
        sent_at: Math.floor(Date.now() / 1000),
      });
      console.error('Failed to execute outgoing command:', commandId, error.message || error);
    }
  } catch (error) {
    const status = error.response ? error.response.status : 'no_response';
    console.warn('Unable to poll outbound commands:', status, error.message || error);
  }
}

const client = new Client({
  authStrategy: new LocalAuth({ clientId: WA_CLIENT_ID }),
  authTimeoutMs: 60000,
  puppeteer: {
    headless: true,
    args: [
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
    ],
  },
});

client.on('qr', (qr) => {
  qrcode.generate(qr, { small: true });
  console.log('Scan the QR code above to connect WhatsApp Web.');
});

client.on('ready', () => {
  console.log('WhatsApp client is ready.');
  setInterval(() => {
    pollAndExecuteOutboundCommands().catch((error) => {
      console.error('Command polling loop error:', error.message || error);
    });
  }, COMMAND_POLL_INTERVAL_MS);
});

client.on('auth_failure', (message) => {
  console.error('WhatsApp auth failure:', message);
});

client.on('disconnected', (reason) => {
  console.warn('WhatsApp client disconnected:', reason);
});

client.on('message', async (msg) => {
  console.log('Received message:', {
    from: msg.from,
    author: msg.author,
    body: msg.body,
    timestamp: msg.timestamp,
  });

  if (!msg.from || !msg.from.includes('@g.us')) {
    return;
  }

  let groupName = msg.from;
  try {
    const chat = await msg.getChat();
    if (chat && chat.name) {
      groupName = chat.name;
    }
  } catch (error) {
    console.warn('Unable to resolve group name. Falling back to group_id.', error.message);
  }

  const payload = {
    text: msg.body,
    sender: msg.author || msg.from,
    group_id: msg.from,
    group_name: groupName,
    timestamp: msg.timestamp,
    wa_message_id: getMessageSerializedId(msg),
    metadata: {
      type: msg.type || null,
      has_media: Boolean(msg.hasMedia),
      from_me: Boolean(msg.fromMe),
      to: msg.to || null,
      mentioned_ids: Array.isArray(msg.mentionedIds) ? msg.mentionedIds : [],
    },
  };

  try {
    await axios.post(BACKEND_URL, payload);
    console.log('API success: message stored in backend.');
  } catch (error) {
    const status = error.response ? error.response.status : 'no_response';
    const details = error.response ? error.response.data : error.message;
    console.error('API failure while storing message:', { status, details });
  }
});

client.on('message_reaction', async (reaction) => {
  try {
    const payload = {
      wa_message_id: normalizeId(reaction.msgId || reaction.id || reaction.parentMsgKey),
      reactor: normalizeId(reaction.senderId || reaction.author || reaction.actor || 'unknown'),
      emoji: String(reaction.reaction || reaction.emoji || ''),
      event_type: reaction.orphan === 1 || reaction.orphan === true ? 'remove' : 'add',
      group_id: normalizeId(reaction.chatId || (reaction.msgId && reaction.msgId.remote) || null),
      group_name: null,
      timestamp: Number.isFinite(reaction.timestamp)
        ? reaction.timestamp
        : Math.floor(Date.now() / 1000),
    };

    if (!payload.wa_message_id || !payload.emoji) {
      return;
    }

    await axios.post(BACKEND_REACTIONS_URL, payload);
    console.log('Reaction ingested:', {
      wa_message_id: payload.wa_message_id,
      reactor: payload.reactor,
      emoji: payload.emoji,
      event_type: payload.event_type,
    });
  } catch (error) {
    const status = error.response ? error.response.status : 'no_response';
    const details = error.response ? error.response.data : error.message;
    console.error('Reaction ingest failure:', { status, details });
  }
});

async function initializeClientWithRetry() {
  for (let attempt = 1; attempt <= MAX_INIT_RETRIES; attempt += 1) {
    try {
      console.log(`Initializing WhatsApp client (attempt ${attempt}/${MAX_INIT_RETRIES})...`);
      await client.initialize();
      return;
    } catch (error) {
      if (isExecutionContextDestroyed(error) && attempt < MAX_INIT_RETRIES) {
        const backoffMs = Math.min(1000 * (2 ** (attempt - 1)), 10000);
        console.warn(`Transient browser context error. Retrying in ${backoffMs}ms...`);
        await sleep(backoffMs);
        continue;
      }

      console.error('Failed to initialize WhatsApp client.');
      console.error(error);
      console.error('If this keeps happening, clear local auth state with: rm -rf .wwebjs_auth');
      process.exit(1);
    }
  }
}

initializeClientWithRetry();
