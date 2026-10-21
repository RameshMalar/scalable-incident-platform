"""Separate authenticated Admin interface; the backend repeats every check."""
import json
import os
from uuid import UUID
import streamlit as st

from access_api import AccessAPIUnavailable, request_access
from access_roles import BOOTSTRAP_ADMIN_ID, InvalidAccess, Role, parse_grant
from dashboard_security import AccessDenied


def require_admin(require_operator):
    context=require_operator()
    if not getattr(context,'can_manage_access',False):
        raise AccessDenied('Platform Admin access is required')
    return context


def render_admin_access(require_operator, get_access_token):
    try:context=require_admin(require_operator)
    except AccessDenied:
        st.error('Platform Admin access is required.')
        return
    st.subheader('Admin Access Management')
    st.caption('Application registrations and tenant permissions only. No Azure account, directory user or Azure resource role is created here.')
    if os.environ.get('ENABLE_CENTRAL_ACCESS','').lower() != 'true':
        st.info('Central access management is disabled until the authoritative Subscription API extension is approved and verified.')
        return
    try:
        token=get_access_token()
        cursor=st.session_state.get('admin_access_cursor')
        response=request_access('GET','/api/v1/admin/access/users',token,cursor=cursor)
        if response.get('contract')!='central-access-v1' or not isinstance(response.get('items'),list):
            raise AccessAPIUnavailable('Access contract not verified')
        records=[parse_grant(item,context.directory_id,context.tenants,active=False) for item in response['items']]
        st.dataframe([record.public() for record in records],hide_index=True,width='stretch')
        first,next_page=st.columns(2)
        if first.button('First registrations page',disabled=cursor is None):
            st.session_state.pop('admin_access_cursor',None)
            st.rerun()
        next_cursor=response.get('next_cursor')
        if next_cursor:UUID(next_cursor)
        if next_page.button('Next registrations page',disabled=not next_cursor):
            st.session_state['admin_access_cursor']=next_cursor
            st.rerun()
        if context.user_id==BOOTSTRAP_ADMIN_ID:
            with st.form('admin_register_user',clear_on_submit=True):
                st.markdown('**Approve a team member**')
                st.caption('A UUID alone is insufficient. The API must verify a fresh Entra identity receipt before accepting your approval.')
                subject=st.text_input('Existing Entra user object ID')
                role=st.selectbox('Application role',[role.value for role in Role],index=2)
                tenants=st.multiselect('Approved platform tenants',context.tenants)
                approval=st.text_input('Approval reference or reason')
                confirm=st.checkbox('I explicitly approve registering this user with this role and tenant scope.')
                register=st.form_submit_button('Approve and register user')
            if register:
                require_admin(require_operator)
                UUID(subject)
                if subject==BOOTSTRAP_ADMIN_ID or not confirm or not approval.strip() or not tenants:
                    raise AccessDenied('Explicit valid user approval is required')
                result=request_access('POST','/api/v1/admin/access/users',get_access_token(),dict(
                    object_id=subject,role=role,tenants=tenants,approval_reference=approval.strip()))
                parse_grant(result,context.directory_id,context.tenants,expected_user=subject)
                st.success('User registration confirmed by the authoritative registry. Refresh the list to review it.')
        else:
            st.caption('New team-member approval is reserved for the original approved Platform Admin.')
        with st.form('admin_change_access',clear_on_submit=True):
            st.markdown('**Review, change, disable or revoke access**')
            subject=st.text_input('Target Entra user object ID',key='access_target')
            expected=st.number_input('Current registry version',min_value=1,step=1)
            action=st.selectbox('Action',['Change role / tenants','Disable','Revoke','Re-enable'])
            role=st.selectbox('Replacement role',[role.value for role in Role],index=2,key='replacement_role')
            tenants=st.multiselect('Replacement tenant scope',context.tenants,key='replacement_tenants')
            reason=st.text_input('Change reason',key='change_reason')
            confirm=st.checkbox('I explicitly approve this change. Disabling/revocation must also block notification eligibility.')
            change=st.form_submit_button('Apply access change')
        if change:
            require_admin(require_operator)
            UUID(subject)
            if subject==BOOTSTRAP_ADMIN_ID or not confirm or not reason.strip():
                raise AccessDenied('The approved Platform Admin mapping is protected')
            payload={'expected_version':int(expected),'reason':reason.strip()}
            if action=='Change role / tenants':
                if not tenants:raise AccessDenied('Select an approved tenant scope')
                payload.update(role=role,tenants=tenants)
            else:payload['status']={'Disable':'disabled','Revoke':'revoked','Re-enable':'active'}[action]
            result=request_access('PATCH','/api/v1/admin/access/users/'+subject,get_access_token(),payload)
            parse_grant(result,context.directory_id,context.tenants,expected_user=subject,active=False)
            st.success('Access change confirmed by the authoritative registry.')
    except (AccessAPIUnavailable,AccessDenied,InvalidAccess,ValueError,TypeError,KeyError):
        st.error('Access management is unavailable or the change was rejected. No change is confirmed; refresh the registry before retrying.')
    render_destination_review(context,require_operator,get_access_token)


APPROVAL_FIELDS={'id','tenant_id','channel','telegram_chat_id','enabled'}


def validated_review(response,tenant_id):
    """Accept only the destination-review-v1 shape for the requested tenant."""
    if (response.get('contract')!='destination-review-v1' or response.get('tenant_id')!=tenant_id
            or not isinstance(response.get('items'),list)):
        raise AccessAPIUnavailable('Destination review contract not verified')
    items=[]
    for item in response['items']:
        record=item['approval_record']
        chat=record['telegram_chat_id']
        UUID(item['object_id'])
        if (set(record)!=APPROVAL_FIELDS or record['tenant_id']!=tenant_id or record['channel']!='telegram'
                or record['enabled'] is not True or not isinstance(chat,str) or not chat.lstrip('-').isdigit()
                or record['id']!='telegram:'+chat):
            raise AccessAPIUnavailable('Invalid approval record')
        items.append(item)
    return items


def render_destination_review(context,require_operator,get_access_token):
    st.markdown('**Telegram destinations awaiting approval**')
    st.caption('Verified team members whose private Telegram chat is not yet an approved destination. '
               'This page cannot approve a destination: an operator records approval in the separate '
               'approved-destinations store, exactly as before. Loading the list is audited.')
    if os.environ.get('ENABLE_TELEGRAM_ONBOARDING','').lower()!='true':
        st.info('Telegram onboarding is disabled.')
        return
    tenant_id=st.selectbox('Tenant to review',context.tenants,key='destination_review_tenant')
    if not st.button('Load verified destinations for review'):
        return
    try:
        require_admin(require_operator)
        if tenant_id not in context.tenants:
            raise AccessDenied('Tenant not in your scope')
        items=validated_review(request_access('GET','/api/v1/admin/telegram/pending-destinations',
                                              get_access_token(),tenant_id=tenant_id),tenant_id)
    except (AccessAPIUnavailable,AccessDenied,ValueError,TypeError,KeyError):
        st.error('Destination review is unavailable. Nothing was approved.')
        return
    if not items:
        st.success('No verified destinations are awaiting approval for this tenant.')
        return
    st.dataframe([{'object_id':item['object_id'],'role':item.get('role'),'verified_at':item.get('verified_at'),
                   'chat':item.get('chat_hint')} for item in items],hide_index=True,width='stretch')
    for item in items:
        with st.expander(f"Approval record for {item['object_id']}"):
            st.caption('Confirm this person owns the chat and belongs to the tenant before the operator writes it.')
            st.code(json.dumps(item['approval_record'],indent=2),language='json')
