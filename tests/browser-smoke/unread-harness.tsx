// Test-only mounted provider harness: shares one QueryClient across account remounts.
import React, {useState} from 'react';
import {createRoot} from 'react-dom/client';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {auth} from '../../apps/miniapp/src/api/client';
import {MessagesUnreadProvider, useMessagesUnread} from '../../apps/miniapp/src/messages-unread';
const client=new QueryClient();
Object.assign(window,{unreadHarnessClient:client});
function Count(){const state=useMessagesUnread();return <output aria-label="unread owner">{state.userId}:{state.total}</output>;}
function Harness(){const [owner,setOwner]=useState('account-a');return <><button onClick={async()=>{history.replaceState(null,'','?e2eUserId=2');await auth();setOwner('account-b');}}>Switch account</button><MessagesUnreadProvider key={owner} userId={owner}><Count/></MessagesUnreadProvider></>;}
await auth();
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><Harness/></QueryClientProvider>);
