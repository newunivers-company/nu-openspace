import { lazy, Suspense } from 'react';
import { Navigate, RouterProvider, createBrowserRouter } from 'react-router-dom';
import MainLayout from './layouts/MainLayout';

const DashboardPage = lazy(() => import('./pages/DashboardPage'));
const EvolutionPage = lazy(() => import('./pages/EvolutionPage'));
const SkillsPage = lazy(() => import('./pages/SkillsPage'));
const SkillDetailPage = lazy(() => import('./pages/SkillDetailPage'));
const WorkflowsPage = lazy(() => import('./pages/WorkflowsPage'));
const WorkflowDetailPage = lazy(() => import('./pages/WorkflowDetailPage'));
const AgentTracePage = lazy(() => import('./pages/AgentTracePage'));

const router = createBrowserRouter([
  {
    path: '/',
    element: <MainLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <DashboardPage /> },
      { path: 'evolution', element: <EvolutionPage /> },
      { path: 'skills', element: <SkillsPage /> },
      { path: 'skills/:skillId', element: <SkillDetailPage /> },
      { path: 'workflows', element: <WorkflowsPage /> },
      { path: 'workflows/:workflowId', element: <WorkflowDetailPage /> },
      { path: 'workflows/:workflowId/trace', element: <AgentTracePage /> },
    ],
  },
]);

export default function App() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center text-sm text-slate-500">
          Loading OpenSpace…
        </div>
      }
    >
      <RouterProvider router={router} />
    </Suspense>
  );
}
