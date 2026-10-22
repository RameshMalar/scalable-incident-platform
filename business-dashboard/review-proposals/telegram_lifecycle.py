"""Offline-testable Subscription API proposal; NOT a running webhook/service.

The store port must atomically consume a challenge and bind its private owner,
and atomically persist preference versions. Existing routing remains the sender.
Never return private destinations or tokens from the public account contract.
"""
import hashlib
import hmac
import secrets
import time

from central_registry import RegistryDenied


class TelegramLifecycle:
    def __init__(self, registry, store, bot_name, webhook_secret, event_types, clock=time.time):
        if not bot_name or not bot_name.replace('_','').isalnum():raise ValueError('Invalid bot name')
        if len(webhook_secret)<32:raise ValueError('A strong webhook secret is required')
        self.registry,self.store,self.bot_name=registry,store,bot_name
        self.webhook_secret=webhook_secret
        self.events=frozenset(event_types)
        self.clock=clock

    async def _owner(self,user,directory,tenant):
        return await self.registry.authorize(user,directory,'Subscriptions.SelfManage',tenant)

    async def status(self,user,directory,tenant):
        await self._owner(user,directory,tenant)
        record=await self.store.preferences(user,tenant) or {}
        return dict(contract='self-service-v1',tenant_id=tenant,
                    verified=bool(record.get('verified')),enabled=bool(record.get('enabled')),
                    event_types=record.get('event_types',[]),supported_event_types=sorted(self.events))

    async def challenge(self,user,directory,tenant):
        await self._owner(user,directory,tenant)
        token=secrets.token_urlsafe(32)  # Telegram /start payload <=64 characters.
        expires=int(self.clock())+300
        digest=hashlib.sha256(token.encode()).hexdigest()
        await self.store.issue(digest,dict(user=user,directory=directory,tenant=tenant,expires=expires))
        return dict(deep_link=f'https://t.me/{self.bot_name}?start={token}',expires_at=expires)

    async def verify_webhook(self,secret,update):
        if not isinstance(secret,str) or not hmac.compare_digest(secret,self.webhook_secret):
            raise RegistryDenied('Webhook denied')
        message=update.get('message',{})
        sender=message.get('from',{})
        chat=message.get('chat',{})
        text=message.get('text','')
        if (chat.get('type')!='private' or type(sender.get('id')) is not int or sender.get('is_bot') is not False
                or chat.get('id')!=sender['id'] or not isinstance(text,str) or not text.startswith('/start ')):
            raise RegistryDenied('A private bot ownership confirmation is required')
        token=text[7:]
        if len(token)!=43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
            raise RegistryDenied('Invalid ownership challenge')
        digest=hashlib.sha256(token.encode()).hexdigest()
        proof=await self.store.challenge(digest)
        if not proof or proof['expires']<=self.clock():raise RegistryDenied('Ownership challenge expired or consumed')
        await self._owner(proof['user'],proof['directory'],proof['tenant'])
        # Store must CAS challenge unused+unexpired and bind destination in one
        # transaction, enforce unique owner, and preserve enabled=False. It must
        # NOT send a message or create notification consent implicitly.
        await self.store.consume_and_bind(digest,proof,chat['id'],int(self.clock()))

    async def preferences(self,user,directory,tenant,enabled,event_types):
        await self._owner(user,directory,tenant)
        if type(enabled) is not bool or not isinstance(event_types,list) or not set(event_types).issubset(self.events):
            raise ValueError('Invalid notification preferences')
        before=await self.store.preferences(user,tenant) or {}
        if enabled and (not before.get('verified') or not before.get('destination_approved')):
            raise RegistryDenied('Verified approved ownership is required')
        # Idempotent self-service PUT; no arbitrary recipient/chat input.
        await self.store.save_preferences(user,tenant,before,enabled,sorted(set(event_types)))
        return await self.status(user,directory,tenant)

    async def eligible(self,user,directory,tenant,event_type,purpose):
        # Both publishing and dispatch must call this fresh; queued envelopes
        # are not evidence of continued consent or access. Outages propagate to
        # existing retry/DLQ handling rather than granting or silently sending.
        await self._owner(user,directory,tenant)
        record=await self.store.preferences(user,tenant) or {}
        return bool(purpose=='production' and record.get('verified') and record.get('destination_approved')
                    and record.get('enabled') and event_type in record.get('event_types',[]))
