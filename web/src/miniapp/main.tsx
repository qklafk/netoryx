import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { isCabinet } from '../shared/api';
import { App } from './App';
import { CabinetRoot } from './cabinet';
import './style.css';

const cabinet = isCabinet();
createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter basename={cabinet ? '/cabinet' : '/app'}>{cabinet ? <CabinetRoot app={<App/>}/> : <App/>}</BrowserRouter></React.StrictMode>);
