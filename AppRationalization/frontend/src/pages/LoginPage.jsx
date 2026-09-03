// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: AppRationalization — frontend/src/pages (LoginPage.jsx)
// Date: 2026-03-20
// ---------------------------------------------------------------------------
import React, { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  Factory,
  Sparkles,
  ShieldCheck,
} from 'lucide-react';

import { useAuth } from '../context/AuthContext';
import { fetchOauthProviders, getGithubAuthUrl, getGoogleAuthUrl } from '../services/authApi';

// Function: LoginPage
const LoginPage = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { login, isAuthenticated, oauthError } = useAuth();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [providerState, setProviderState] = useState({
    google: { enabled: false },
    github: { enabled: false },
  });

  const redirectTo = useMemo(() => {
    const from = location.state?.from?.pathname;
    return from && from !== '/login' ? from : '/launch-modules';
  }, [location.state]);

  useEffect(() => {
    if (isAuthenticated) {
      navigate('/launch-modules', { replace: true });
    }
  }, [isAuthenticated, navigate]);

  useEffect(() => {
    let active = true;
    fetchOauthProviders()
      .then((data) => {
        if (active) {
          setProviderState(data);
        }
      })
      .catch(() => {
        if (active) {
          setProviderState({
            google: { enabled: false },
            github: { enabled: false },
          });
        }
      });

    return () => {
      active = false;
    };
  }, []);

  // Function: handleLogin
  const handleLogin = async (event) => {
    event.preventDefault();
    setError('');
    setLoading(true);

    try {
      await login(username, password);
      navigate(redirectTo, { replace: true });
    } catch (err) {
      setError(err?.response?.data?.error || 'Login failed. Check username and password.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="az-shell login-shell flex items-center justify-center px-4 py-6 sm:px-6 lg:px-8">
      <div className="relative z-10 grid w-full max-w-[1460px] gap-6 xl:grid-cols-[1.12fr_0.88fr]">
        <section className="az-hero flex min-h-[420px] flex-col justify-center overflow-hidden sm:p-8 lg:p-9">
          <div className="flex flex-wrap gap-2">
            <span className="az-hero-chip">
              <Sparkles size={14} />
              Purpose-built workspace access
            </span>
            <span className="az-hero-chip">
              <ShieldCheck size={14} />
              Role aware routing
            </span>
          </div>

          <div className="mt-6 lg:mt-7">
            <div className="max-w-2xl">
              <p className="az-hero-badge">Unified launch point</p>
              <h1
                className="mt-3 max-w-3xl text-xl font-semibold leading-[1.25] tracking-tight sm:text-2xl"
                style={{ textAlign: 'justify' }}
              >
                A modern control room for portfolio, modernization, and operations work.
              </h1>
            </div>
          </div>
        </section>

        <section className="az-login-panel">
          <div className="flex items-center justify-between gap-4">
            <div>
              <p className="az-panel-eyebrow">Portal access</p>
              <h2 className="mt-3 text-2xl font-semibold" style={{ color: 'var(--az-text)' }}>Sign in</h2>
              <p className="mt-2 max-w-md text-sm leading-6" style={{ color: 'var(--az-text-muted)' }}>
                Enter your credentials to continue into the workspace launcher.
              </p>
            </div>
            <div className="az-logo-mark hidden sm:flex" style={{ width: 40, height: 40 }}>
              <Factory size={18} />
            </div>
          </div>

          {(error || oauthError) && (
            <div className="az-alert az-alert-error mt-6">
              {error || `OAuth sign-in failed: ${oauthError}`}
            </div>
          )}

          <form className="mt-7 space-y-4" onSubmit={handleLogin}>
            <div>
              <label className="mb-1.5 block text-sm font-medium" style={{ color: 'var(--az-text)' }} htmlFor="username">
                Username
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
                className="az-field"
                placeholder="Enter your username"
              />
            </div>

            <div>
              <label className="mb-1.5 block text-sm font-medium" style={{ color: 'var(--az-text)' }} htmlFor="password">
                Password
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                required
                className="az-field"
                placeholder="Enter your password"
              />
            </div>

            <button type="submit" disabled={loading} className="az-btn az-btn-primary w-full">
              {loading ? 'Signing in...' : 'Enter workspace'}
              {!loading && <ArrowRight size={16} />}
            </button>
          </form>

          <div className="az-divider-row mt-6">or continue with</div>

          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              disabled={!providerState.google?.enabled}
              onClick={() => {
                window.location.href = getGoogleAuthUrl();
              }}
              className="az-btn az-btn-secondary"
            >
              Continue with Google
            </button>
            <button
              type="button"
              disabled={!providerState.github?.enabled}
              onClick={() => {
                window.location.href = getGithubAuthUrl();
              }}
              className="az-btn az-btn-secondary"
            >
              Continue with GitHub
            </button>
          </div>
        </section>
      </div>
    </div>
  );
};

export default LoginPage;
