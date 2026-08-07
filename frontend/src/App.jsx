import React, { useState, useEffect, useRef } from 'react';

const API_BASE = "http://localhost:8000/api";
const STORAGE_BASE = "http://localhost:8000/storage";

export default function App() {
  const [channels, setChannels] = useState([]);
  const [activeChannel, setActiveChannel] = useState(null);
  const [channelVideos, setChannelVideos] = useState([]);
  const [view, setView] = useState('dashboard'); // 'dashboard', 'wizard', 'channel_detail'
  const [selectedVideo, setSelectedVideo] = useState(null);
  const [showSubmitModal, setShowSubmitModal] = useState(false);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [showProfileModal, setShowProfileModal] = useState(false);
  const [showChannelPickerModal, setShowChannelPickerModal] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // Database Connection Info
  const [dbInfo, setDbInfo] = useState({
    status: 'connected',
    engine: 'postgresql',
    service_name: 'Supabase PostgreSQL (Self-Hosted VPS rooseveltvps)',
    supabase_url: 'https://bd.izivoice.app',
    database_host: '31.97.118.192:5432'
  });

  const fetchDbStatus = async () => {
    try {
      const res = await fetch(`${API_BASE}/db-status`);
      if (res.ok) {
        const data = await res.json();
        setDbInfo(data);
      }
    } catch (e) {
      console.error("Error fetching db status:", e);
    }
  };

  useEffect(() => {
    fetchDbStatus();
  }, []);

  // Theme State ('dark' | 'light')
  const [themeMode, setThemeMode] = useState(() => {
    return localStorage.getItem("nichecut_theme") || "dark";
  });

  useEffect(() => {
    localStorage.setItem("nichecut_theme", themeMode);
    if (themeMode === "light") {
      document.documentElement.classList.remove("dark");
    } else {
      document.documentElement.classList.add("dark");
    }
  }, [themeMode]);

  // User Auth State
  const [currentUser, setCurrentUser] = useState(() => {
    const saved = localStorage.getItem("nichecut_user");
    return saved ? JSON.parse(saved) : null;
  });
  const [authTab, setAuthTab] = useState('login'); // 'login' | 'register' | 'forgot'
  const [authForm, setAuthForm] = useState({ email: '', password: '' });
  const [showPassword, setShowPassword] = useState(false);
  const [resetSuccessMsg, setResetSuccessMsg] = useState('');

  // Profile Settings Form State
  const [profileTab, setProfileTab] = useState('database'); // 'database' | 'security' | 'appearance'
  const [passwordForm, setPasswordForm] = useState({ old_password: '', new_password: '', confirm_password: '' });
  const [enable2FA, setEnable2FA] = useState(false);

  // Submission Form State (Strictly 2 Modes)
  const [submitMode, setSubmitMode] = useState('text'); // 'text' | 'audio_upload'
  const [singleScriptText, setSingleScriptText] = useState('');
  const [audioFilesList, setAudioFilesList] = useState([]);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef(null);

  // Wizard state
  const [wizardStep, setWizardStep] = useState(1);
  const [wizardMode, setWizardMode] = useState('create'); // 'create' | 'edit'
  const [editingChannelId, setEditingChannelId] = useState(null);
  const [logoFile, setLogoFile] = useState(null);
  const [logoPreviewUrl, setLogoPreviewUrl] = useState(null);
  const logoInputRef = useRef(null);
  const defaultChannelForm = {
    name: '',
    niche: 'Philosophie & Stoïcisme',
    subtitle_style: {
      font: 'Arial',
      size: 44,
      color: '&H00FFFFFF',
      outline_color: '&H00000000',
      outline_width: 3,
      position: 'bottom',
      karaoke: true
    },
    branding: {
      channel_name_text: '',
      logo_path: ''
    },
    music_preference: {
      enabled: true,
      track_id_or_style: 'ambient',
      volume: 0.15
    },
    image_style: {
      source: 'library',
      style_prompt: 'cinematic dramatic lighting, high detail',
      library_path: ''
    },
    effects_config: {
      grain: true,
      color_grade: 'warm',
      zoom_min_pct: 1.0,
      zoom_max_pct: 1.15
    }
  };
  const [newChannel, setNewChannel] = useState(defaultChannelForm);

  const fetchChannels = async () => {
    try {
      const res = await fetch(`${API_BASE}/channels`);
      if (res.ok) {
        const data = await res.json();
        setChannels(data);
      }
    } catch (e) {
      console.error("API error loading channels:", e);
    }
  };

  const fetchChannelVideos = async (channelId) => {
    try {
      const res = await fetch(`${API_BASE}/videos/channel/${channelId}`);
      if (res.ok) {
        const data = await res.json();
        setChannelVideos(data);
      }
    } catch (e) {
      console.error("API error loading channel videos:", e);
    }
  };

  useEffect(() => {
    fetchChannels();
    const interval = setInterval(() => {
      fetchChannels();
      if (activeChannel) {
        fetchChannelVideos(activeChannel.id);
      }
    }, 4000);
    return () => clearInterval(interval);
  }, [activeChannel]);

  const handleAuthSubmit = async (e) => {
    e.preventDefault();
    if (!authForm.email || !authForm.password) return alert("Veuillez remplir l'email et le mot de passe.");

    if (authTab === 'forgot') {
      try {
        setLoading(true);
        const res = await fetch(`${API_BASE}/auth/reset-password`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email: authForm.email, new_password: authForm.password })
        });
        if (res.ok) {
          setResetSuccessMsg("Mot de passe réinitialisé avec succès ! Connectez-vous.");
          setAuthTab('login');
        } else {
          const err = await res.json();
          alert(err.detail || "Erreur de réinitialisation.");
        }
      } catch (err) {
        alert("Erreur réseau: " + err.message);
      } finally {
        setLoading(false);
      }
      return;
    }

    const endpoint = authTab === 'register' ? `${API_BASE}/auth/register` : `${API_BASE}/auth/login`;
    try {
      setLoading(true);
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(authForm)
      });
      if (res.ok) {
        const user = await res.json();
        setCurrentUser(user);
        localStorage.setItem("nichecut_user", JSON.stringify(user));
        setShowAuthModal(false);
        setAuthForm({ email: '', password: '' });
      } else {
        const err = await res.json();
        alert(err.detail || "Erreur d'authentification.");
      }
    } catch (err) {
      alert("Erreur réseau: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleGoogleAuth = () => {
    alert("Connexion via Google : Redirection OAuth vers Google en cours...");
  };

  const handleChangePasswordSubmit = async (e) => {
    e.preventDefault();
    if (!currentUser) return;
    if (passwordForm.new_password !== passwordForm.confirm_password) {
      return alert("Le nouveau mot de passe et sa confirmation ne correspondent pas.");
    }
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/auth/change-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          user_id: currentUser.id,
          old_password: passwordForm.old_password,
          new_password: passwordForm.new_password
        })
      });
      if (res.ok) {
        alert("Mot de passe modifié avec succès !");
        setPasswordForm({ old_password: '', new_password: '', confirm_password: '' });
      } else {
        const err = await res.json();
        alert(err.detail || "Erreur lors du changement de mot de passe.");
      }
    } catch (err) {
      alert("Erreur réseau: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleLogout = () => {
    setCurrentUser(null);
    localStorage.removeItem("nichecut_user");
    setShowProfileModal(false);
  };

  const resetWizardState = () => {
    setNewChannel(defaultChannelForm);
    setWizardMode('create');
    setEditingChannelId(null);
    setLogoFile(null);
    setLogoPreviewUrl(null);
    setWizardStep(1);
  };

  const openCreateWizard = () => {
    resetWizardState();
    setView('wizard');
  };

  const openEditWizard = (channel, e) => {
    if (e) e.stopPropagation();
    setWizardMode('edit');
    setEditingChannelId(channel.id);
    setNewChannel({
      name: channel.name || '',
      niche: channel.niche || 'Philosophie & Stoïcisme',
      subtitle_style: { ...defaultChannelForm.subtitle_style, ...(channel.subtitle_style || {}) },
      branding: { ...defaultChannelForm.branding, ...(channel.branding || {}) },
      music_preference: { ...defaultChannelForm.music_preference, ...(channel.music_preference || {}) },
      image_style: { ...defaultChannelForm.image_style, ...(channel.image_style || {}) },
      effects_config: { ...defaultChannelForm.effects_config, ...(channel.effects_config || {}) }
    });
    setLogoFile(null);
    setLogoPreviewUrl(channel.branding?.logo_path ? `${STORAGE_BASE}/${channel.branding.logo_path}` : null);
    setWizardStep(1);
    setView('wizard');
  };

  const handleLogoFileSelect = (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    setLogoFile(file);
    setLogoPreviewUrl(URL.createObjectURL(file));
  };

  const uploadChannelLogo = async (channelId) => {
    if (!logoFile) return;
    const formData = new FormData();
    formData.append("file", logoFile);
    await fetch(`${API_BASE}/channels/${channelId}/logo`, { method: 'POST', body: formData });
  };

  const handleSaveChannel = async () => {
    if (!newChannel.name) return alert("Veuillez saisir un nom de chaîne.");
    try {
      setLoading(true);
      let saved;
      if (wizardMode === 'edit' && editingChannelId) {
        const res = await fetch(`${API_BASE}/channels/${editingChannelId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(newChannel)
        });
        if (!res.ok) throw new Error((await res.json()).detail || "Erreur de mise à jour.");
        saved = await res.json();
      } else {
        const url = currentUser ? `${API_BASE}/channels?user_id=${currentUser.id}` : `${API_BASE}/channels`;
        const res = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(newChannel)
        });
        if (!res.ok) throw new Error((await res.json()).detail || "Erreur de création.");
        saved = await res.json();
      }

      if (logoFile) {
        await uploadChannelLogo(saved.id);
      }

      await fetchChannels();
      setActiveChannel(saved);
      setView('channel_detail');
      fetchChannelVideos(saved.id);
      resetWizardState();
    } catch (e) {
      alert("Erreur lors de l'enregistrement de la chaîne: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  const getChannelLogoUrl = (channel) => channel?.branding?.logo_path ? `${STORAGE_BASE}/${channel.branding.logo_path}` : null;

  const getChannelStatusInfo = (channel) => {
    const rendering = channel.rendering_count || 0;
    const queued = channel.queued_count || 0;
    const done = channel.done_count || 0;
    const failed = channel.failed_count || 0;
    if (rendering > 0) return { label: 'Rendu en cours', className: 'bg-blue-950 text-blue-300 border border-blue-800 animate-pulse' };
    if (queued > 0) return { label: 'En file d\'attente', className: 'bg-yellow-950 text-yellow-300 border border-yellow-800' };
    if (done > 0) return { label: 'Prêt', className: 'bg-emerald-950 text-emerald-300 border border-emerald-800' };
    if (failed > 0) return { label: 'Échec de rendu', className: 'bg-red-950 text-red-300 border border-red-800' };
    return { label: 'Aucune vidéo', className: 'bg-surface-container-high text-on-surface-variant border border-surface-container-highest' };
  };

  const handleDragOver = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  };

  const handleDragLeave = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    const droppedFiles = Array.from(e.dataTransfer.files).filter(f => 
      f.type.startsWith('audio/') || f.type.startsWith('video/') || /\.(mp3|wav|m4a|mp4|aac|flac|ogg)$/i.test(f.name)
    );

    if (droppedFiles.length > 0) {
      setAudioFilesList(prev => [...prev, ...droppedFiles]);
    }
  };

  const handleFileSelect = (e) => {
    const selected = Array.from(e.target.files);
    if (selected.length > 0) {
      setAudioFilesList(prev => [...prev, ...selected]);
    }
  };

  const removeAudioFile = (index) => {
    setAudioFilesList(prev => prev.filter((_, i) => i !== index));
  };

  const handleSubjectSubmit = async () => {
    if (!activeChannel) return alert("Veuillez sélectionner une chaîne.");

    const formData = new FormData();
    formData.append("channel_id", activeChannel.id);
    formData.append("input_type", submitMode === 'audio_upload' ? 'audio' : 'text');

    if (submitMode === 'text') {
      if (!singleScriptText.trim()) return alert("Veuillez saisir le texte de votre script.");
      formData.append("script_text", singleScriptText.trim());
    } else if (submitMode === 'audio_upload') {
      if (audioFilesList.length === 0) return alert("Veuillez glisser-déposer au moins un fichier audio.");
      audioFilesList.forEach(file => {
        formData.append("audio_files", file);
      });
    }

    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/videos`, {
        method: 'POST',
        body: formData
      });
      if (res.ok) {
        setSingleScriptText('');
        setAudioFilesList([]);
        setShowSubmitModal(false);
        fetchChannelVideos(activeChannel.id);
        fetchChannels();
      } else {
        const err = await res.json();
        alert(err.detail || "Erreur lors de l'envoi.");
      }
    } catch (e) {
      alert("Erreur réseau: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRetryVideo = async (videoId) => {
    try {
      await fetch(`${API_BASE}/videos/${videoId}/retry`, { method: 'POST' });
      if (activeChannel) fetchChannelVideos(activeChannel.id);
    } catch (e) {
      console.error(e);
    }
  };

  const handleDeleteChannel = async (channelId, e) => {
    e.stopPropagation();
    if (!confirm("Voulez-vous vraiment supprimer cette chaîne ?")) return;
    try {
      await fetch(`${API_BASE}/channels/${channelId}`, { method: 'DELETE' });
      fetchChannels();
      if (activeChannel && activeChannel.id === channelId) {
        setActiveChannel(null);
        setView('dashboard');
      }
    } catch (err) {
      console.error(err);
    }
  };

  const filteredChannels = channels.filter(c => 
    c.name.toLowerCase().includes(searchQuery.toLowerCase()) || 
    c.niche.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const totalQueued = channels.reduce((acc, c) => acc + (c.queued_count || 0) + (c.rendering_count || 0), 0);
  const totalCompleted = channels.reduce((acc, c) => acc + (c.done_count || 0), 0);

  return (
    <div className="font-body-md antialiased overflow-hidden flex h-screen bg-background text-on-background">
      
      {/* SideNavBar */}
      <nav className="hidden md:flex flex-col bg-surface-container text-primary font-label-bold text-label-bold fixed left-0 top-0 h-screen w-[240px] z-40 border-r border-outline-variant py-xl">
        <div className="px-6 mb-8 flex items-center gap-3 cursor-pointer" onClick={() => setView('dashboard')}>
          <div className="w-8 h-8 rounded-lg bg-primary-container flex items-center justify-center text-on-primary-container font-bold">N</div>
          <div>
            <div className="font-title-sm text-title-sm font-black text-on-surface">NicheCut</div>
            <div className="text-on-surface-variant font-body-sm text-body-sm">Video Automation</div>
          </div>
        </div>

        <div className="flex-1 px-4 space-y-2 overflow-y-auto">
          <button 
            onClick={() => setView('dashboard')}
            className={`w-full flex items-center gap-3 px-4 py-3 cursor-pointer rounded-xl transition-all ${view === 'dashboard' ? 'bg-primary-container text-on-primary-container font-bold' : 'text-on-surface-variant hover:bg-surface-container-high'}`}
          >
            <span className="material-symbols-outlined" style={{ fontVariationSettings: view === 'dashboard' ? "'FILL' 1" : "'FILL' 0" }}>dashboard</span>
            Home
          </button>

          <button
            onClick={() => setView('dashboard')}
            className={`w-full flex items-center gap-3 px-4 py-3 cursor-pointer rounded-xl transition-all ${(view === 'dashboard' || view === 'channel_detail') ? 'bg-primary-container text-on-primary-container font-bold' : 'text-on-surface-variant hover:bg-surface-container-high'}`}
          >
            <span className="material-symbols-outlined" style={{ fontVariationSettings: (view === 'dashboard' || view === 'channel_detail') ? "'FILL' 1" : "'FILL' 0" }}>subscriptions</span>
            Mes Chaînes
          </button>

          {/* Paramètres & Profil placed BEFORE Nouvelle Vidéo */}
          <button 
            onClick={() => {
              if (currentUser) {
                setShowProfileModal(true);
              } else {
                setShowAuthModal(true);
              }
            }}
            className="w-full flex items-center gap-3 px-4 py-3 cursor-pointer rounded-xl transition-all text-on-surface-variant hover:bg-surface-container-high"
          >
            <span className="material-symbols-outlined">settings</span>
            Paramètres & Profil
          </button>

          <button
            onClick={() => {
              if (channels.length === 0) {
                openCreateWizard();
              } else if (channels.length === 1) {
                setActiveChannel(channels[0]);
                setShowSubmitModal(true);
              } else {
                setShowChannelPickerModal(true);
              }
            }}
            className="w-full flex items-center gap-3 px-4 py-3 cursor-pointer rounded-xl transition-all text-on-surface-variant hover:bg-surface-container-high"
          >
            <span className="material-symbols-outlined">add_circle</span>
            Nouvelle Vidéo
          </button>
        </div>

        {/* Bottom Sidebar Profile Card */}
        <div className="px-4 mt-auto space-y-3">
          {currentUser ? (
            <div 
              onClick={() => setShowProfileModal(true)}
              className="p-3 bg-surface-container-low hover:bg-surface-container-high rounded-xl border border-surface-container-highest cursor-pointer flex items-center gap-3 transition-all group"
            >
              <div className="w-8 h-8 rounded-full bg-primary-container text-on-primary-container flex items-center justify-center font-bold text-sm">
                {currentUser.name.slice(0, 1).toUpperCase()}
              </div>
              <div className="truncate flex-1">
                <div className="text-xs text-on-surface font-semibold truncate group-hover:text-primary-container">{currentUser.name}</div>
                <div className="text-[10px] text-on-surface-variant truncate">{currentUser.email}</div>
              </div>
              <span className="material-symbols-outlined text-[18px] text-on-surface-variant">tune</span>
            </div>
          ) : (
            <button 
              onClick={() => setShowAuthModal(true)}
              className="w-full py-3 bg-primary-container text-on-primary-container rounded-xl font-label-bold text-xs hover:bg-primary transition-colors flex items-center justify-center gap-2 shadow-md"
            >
              <span className="material-symbols-outlined text-[18px]">account_circle</span>
              Se connecter / S'inscrire
            </button>
          )}
        </div>
      </nav>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col md:ml-[240px] h-screen overflow-hidden bg-background">
        
        {/* Top Header */}
        <div className="hidden md:flex justify-between items-center px-xl py-6 border-b border-outline-variant">
          <h1 className="font-display-lg text-display-lg text-on-surface">
            {view === 'dashboard' && 'Dashboard'}
            {view === 'wizard' && 'Assistant de Création'}
            {view === 'channel_detail' && (activeChannel ? activeChannel.name : 'Détail Chaîne')}
          </h1>
          <div className="flex items-center gap-4">
            
            {/* Supabase VPS Live Database Status Pill */}
            <div 
              onClick={() => setShowProfileModal(true)}
              className="flex items-center gap-2 bg-emerald-950/60 border border-emerald-800 text-emerald-300 px-3 py-1.5 rounded-xl text-xs font-mono cursor-pointer hover:bg-emerald-900/60 transition-colors shadow-sm"
              title={`Base de données: ${dbInfo.service_name} (${dbInfo.database_host})`}
            >
              <div className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></div>
              <span className="font-bold">Supabase VPS</span>
              <span className="text-[10px] opacity-75">({dbInfo.database_host})</span>
            </div>

            <div className="relative focus-glow rounded-xl">
              <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-on-surface-variant" style={{ fontSize: '18px' }}>search</span>
              <input 
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                className="bg-surface-container-low border border-outline-variant rounded-xl pl-10 pr-4 py-2 font-body-sm text-body-sm text-on-surface focus:outline-none w-64 transition-all" 
                placeholder="Rechercher une chaîne..." 
                type="text"
              />
            </div>

            {/* Profile Header Indicator */}
            {currentUser ? (
              <div 
                onClick={() => setShowProfileModal(true)}
                className="flex items-center gap-2 bg-surface-container-high hover:bg-surface-variant px-3 py-1.5 rounded-xl border border-surface-container-highest cursor-pointer transition-all"
              >
                <div className="w-7 h-7 rounded-full bg-primary-container text-on-primary-container flex items-center justify-center font-bold text-xs">
                  {currentUser.name.slice(0, 1).toUpperCase()}
                </div>
                <span className="text-xs text-on-surface font-semibold">{currentUser.name}</span>
              </div>
            ) : (
              <button 
                onClick={() => setShowAuthModal(true)}
                className="px-3 py-1.5 bg-surface-container-high hover:bg-surface-variant text-on-surface rounded-xl text-xs font-label-bold flex items-center gap-1.5 border border-surface-container-highest"
              >
                <span className="material-symbols-outlined text-[16px]">account_circle</span> Connexion
              </button>
            )}

            <button 
              onClick={openCreateWizard}
              className="bg-primary-container text-on-primary-container px-4 py-2 rounded-xl font-label-bold text-label-bold flex items-center gap-2 hover:bg-primary transition-colors"
            >
              <span className="material-symbols-outlined" style={{ fontSize: '18px' }}>add</span>
              Nouvelle Chaîne
            </button>
          </div>
        </div>

        {/* Scrollable Canvas */}
        <div className="flex-1 overflow-y-auto p-gutter md:p-xl">
          <div className="max-w-[1440px] mx-auto space-y-8">
            
            {/* VIEW 1: DASHBOARD */}
            {view === 'dashboard' && (
              <>
                {/* Stats Row Bento */}
                <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  <div className="bg-level-1 rounded-xl p-6 flex flex-col justify-between">
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="font-label-bold text-label-bold text-on-surface-variant uppercase tracking-wider">Chaînes Actives</h3>
                      <span className="material-symbols-outlined text-primary" style={{ fontVariationSettings: "'FILL' 1" }}>cell_tower</span>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="font-display-lg text-display-lg text-on-surface">{channels.length}</span>
                      <div className="flex items-center gap-1 text-primary font-mono-label text-mono-label">
                        <span>Configurées</span>
                      </div>
                    </div>
                    <div className="w-full h-1 bg-surface-container mt-4 rounded-full overflow-hidden">
                      <div className="h-full bg-primary w-full rounded-full"></div>
                    </div>
                  </div>

                  <div className="bg-level-1 rounded-xl p-6 flex flex-col justify-between">
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="font-label-bold text-label-bold text-on-surface-variant uppercase tracking-wider">Vidéos en Attente</h3>
                      <span className="material-symbols-outlined text-tertiary" style={{ fontVariationSettings: "'FILL' 1" }}>hourglass_empty</span>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="font-display-lg text-display-lg text-on-surface">{totalQueued}</span>
                      <div className="flex items-center gap-1 text-on-surface-variant font-mono-label text-mono-label">
                        <span>En cours de rendu...</span>
                      </div>
                    </div>
                    <div className="w-full h-1 bg-surface-container mt-4 rounded-full overflow-hidden flex">
                      <div className="h-full bg-primary-container w-1/2"></div>
                    </div>
                  </div>

                  <div className="bg-level-1 rounded-xl p-6 flex flex-col justify-between">
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="font-label-bold text-label-bold text-on-surface-variant uppercase tracking-wider">Vidéos Terminées</h3>
                      <span className="material-symbols-outlined text-primary-container" style={{ fontVariationSettings: "'FILL' 1" }}>task_alt</span>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="font-display-lg text-display-lg text-on-surface">{totalCompleted}</span>
                      <div className="font-label-bold text-label-bold text-on-surface-variant">Prêtes</div>
                    </div>
                    <div className="w-full h-1 bg-surface-container mt-4 rounded-full overflow-hidden">
                      <div className="h-full bg-primary-container w-full"></div>
                    </div>
                  </div>
                </section>

                {/* Channels Section */}
                <section>
                  <div className="flex justify-between items-center mb-6">
                    <h2 className="font-headline-md text-headline-md text-on-surface">Vos Pipelines de Chaînes</h2>
                  </div>

                  {filteredChannels.length === 0 ? (
                    <div className="bg-level-1 rounded-xl p-12 text-center">
                      <span className="material-symbols-outlined text-[48px] text-on-surface-variant mb-4">subscriptions</span>
                      <h3 className="font-title-sm text-title-sm text-on-surface mb-2">Aucune chaîne configurée</h3>
                      <p className="text-on-surface-variant mb-6">Configurez votre premier pipeline une fois (sous-titres, logo, musique, images) et générez sans limites.</p>
                      <button 
                        onClick={openCreateWizard}
                        className="bg-primary-container text-on-primary-container px-6 py-3 rounded-xl font-label-bold text-label-bold hover:bg-primary transition-colors inline-flex items-center gap-2"
                      >
                        <span className="material-symbols-outlined">add</span> Créer une chaîne
                      </button>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
                      {filteredChannels.map(chan => (
                        <div 
                          key={chan.id} 
                          onClick={() => { setActiveChannel(chan); fetchChannelVideos(chan.id); setView('channel_detail'); }}
                          className="bg-level-1 rounded-xl p-5 hover:bg-surface-container-high transition-colors border border-transparent hover:border-outline-variant cursor-pointer group flex flex-col justify-between min-h-[180px]"
                        >
                          <div className="flex items-start justify-between">
                            <div className="flex items-center gap-3">
                              <div className="w-10 h-10 rounded-xl bg-[#004c66] text-[#c2e8ff] flex items-center justify-center font-bold font-title-sm">
                                {chan.name.slice(0, 2).toUpperCase()}
                              </div>
                              <div>
                                <h4 className="font-title-sm text-title-sm text-on-surface group-hover:text-primary-container transition-colors">{chan.name}</h4>
                                <span className="font-label-bold text-label-bold text-on-surface-variant">{chan.niche}</span>
                              </div>
                            </div>
                            <button 
                              onClick={(e) => handleDeleteChannel(chan.id, e)}
                              className="text-error hover:text-red-400 p-1"
                            >
                              <span className="material-symbols-outlined text-[18px]">delete</span>
                            </button>
                          </div>

                          <div className="grid grid-cols-2 gap-2 mt-4">
                            <div className="bg-surface p-2 rounded-xl border border-surface-container-highest">
                              <div className="font-label-bold text-label-bold text-on-surface-variant mb-1">En File</div>
                              <div className="font-body-md text-body-md text-primary-container font-bold">{(chan.queued_count || 0) + (chan.rendering_count || 0)}</div>
                            </div>
                            <div className="bg-surface p-2 rounded-xl border border-surface-container-highest">
                              <div className="font-label-bold text-label-bold text-on-surface-variant mb-1">Vidéos Prêtes</div>
                              <div className="font-body-md text-body-md text-on-surface font-bold">{chan.done_count || 0}</div>
                            </div>
                          </div>
                        </div>
                      ))}

                      {/* Add Channel Button Card */}
                      <button 
                        onClick={openCreateWizard}
                        className="rounded-xl p-5 border-2 border-dashed border-surface-container-highest hover:border-primary-container hover:bg-surface transition-all flex flex-col items-center justify-center gap-3 min-h-[180px] text-on-surface-variant hover:text-primary-container group"
                      >
                        <div className="w-12 h-12 rounded-full bg-surface-container-low group-hover:bg-primary-container flex items-center justify-center transition-colors">
                          <span className="material-symbols-outlined text-[24px] group-hover:text-on-primary-container">add</span>
                        </div>
                        <span className="font-title-sm text-title-sm">Créer une Chaîne</span>
                      </button>
                    </div>
                  )}
                </section>
              </>
            )}

            {/* VIEW 2: CHANNEL CREATION WIZARD */}
            {view === 'wizard' && (
              <div className="max-w-[800px] mx-auto space-y-6">
                <button 
                  onClick={() => setView('dashboard')}
                  className="text-on-surface-variant hover:text-on-surface flex items-center gap-2 font-label-bold"
                >
                  <span className="material-symbols-outlined">arrow_back</span> Retour
                </button>

                <div className="bg-level-1 rounded-xl p-8 border border-surface-container-highest">
                  <h2 className="font-display-lg text-display-lg text-on-surface mb-2">Assistant de Création</h2>
                  <p className="text-on-surface-variant mb-6">Étape {wizardStep} sur 5 — Configurez l'identité et le style de votre chaîne automatisée.</p>

                  <div className="flex items-center justify-between w-full mb-8 relative">
                    <div className="absolute left-0 top-1/2 -translate-y-1/2 w-full h-[2px] bg-surface-container-high -z-10"></div>
                    {[1, 2, 3, 4, 5].map(step => (
                      <div key={step} className="flex flex-col items-center gap-1">
                        <div className={`w-8 h-8 rounded-full flex items-center justify-center font-label-bold text-label-bold ${wizardStep >= step ? 'bg-primary-container text-on-primary-container' : 'bg-surface-container-high text-outline'}`}>
                          {step}
                        </div>
                      </div>
                    ))}
                  </div>

                  {wizardStep === 1 && (
                    <div className="space-y-4">
                      <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Informations Générales</h3>
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Nom de la chaîne YouTube</label>
                        <input 
                          value={newChannel.name}
                          onChange={e => setNewChannel({ ...newChannel, name: e.target.value })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                          placeholder="Ex: Stoic Mind Daily"
                        />
                      </div>
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Niche de contenu</label>
                        <select 
                          value={newChannel.niche}
                          onChange={e => setNewChannel({ ...newChannel, niche: e.target.value })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                        >
                          <option value="Philosophie & Stoïcisme">Philosophie & Stoïcisme</option>
                          <option value="Spiritualité & Méditation">Spiritualité & Méditation</option>
                          <option value="Religion & Récits Antiquité">Religion & Récits Antiquité</option>
                          <option value="Développement Personnel">Développement Personnel</option>
                        </select>
                      </div>
                    </div>
                  )}

                  {wizardStep === 2 && (
                    <div className="space-y-4">
                      <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Sous-titres & Karaoké ASS</h3>
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <label className="block font-label-bold text-on-surface-variant mb-2">Police (Font)</label>
                          <select 
                            value={newChannel.subtitle_style.font}
                            onChange={e => setNewChannel({ ...newChannel, subtitle_style: { ...newChannel.subtitle_style, font: e.target.value } })}
                            className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                          >
                            <option value="Arial">Arial</option>
                            <option value="Helvetica">Helvetica</option>
                            <option value="Montserrat">Montserrat</option>
                            <option value="Impact">Impact</option>
                          </select>
                        </div>
                        <div>
                          <label className="block font-label-bold text-on-surface-variant mb-2">Taille Font (px)</label>
                          <input 
                            type="number"
                            value={newChannel.subtitle_style.size}
                            onChange={e => setNewChannel({ ...newChannel, subtitle_style: { ...newChannel.subtitle_style, size: parseInt(e.target.value) || 44 } })}
                            className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                          />
                        </div>
                      </div>

                      <div className="mt-6">
                        <label className="block font-label-bold text-on-surface-variant mb-2">Aperçu vidéo en direct</label>
                        <div className="w-full h-48 rounded-xl bg-surface-container-lowest border border-surface-container-highest flex items-center justify-center relative overflow-hidden">
                          <div className="absolute inset-0 bg-gradient-to-t from-black/80 to-transparent"></div>
                          <div style={{
                            fontFamily: newChannel.subtitle_style.font,
                            fontSize: `${newChannel.subtitle_style.size * 0.4}px`,
                            fontWeight: 'bold',
                            color: '#fff',
                            textShadow: '0 2px 6px rgba(0,0,0,0.9)'
                          }} className="relative z-10 text-center px-4">
                            Le calme intérieur dépend de votre esprit
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {wizardStep === 3 && (
                    <div className="space-y-4">
                      <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Branding & Logo</h3>
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Texte Filigrane / Nom de chaîne</label>
                        <input 
                          value={newChannel.branding.channel_name_text}
                          onChange={e => setNewChannel({ ...newChannel, branding: { ...newChannel.branding, channel_name_text: e.target.value } })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                          placeholder="ex: @StoicMindDaily"
                        />
                      </div>
                    </div>
                  )}

                  {wizardStep === 4 && (
                    <div className="space-y-4">
                      <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Musique de Fond & Auto-Ducking</h3>
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Style Musical Ambiant</label>
                        <select 
                          value={newChannel.music_preference.track_id_or_style}
                          onChange={e => setNewChannel({ ...newChannel, music_preference: { ...newChannel.music_preference, track_id_or_style: e.target.value } })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                        >
                          <option value="ambient">Zen & Méditation (Ambiant)</option>
                          <option value="dramatic">Méditatif & Profond</option>
                          <option value="cinematic">Cinématique Épique</option>
                        </select>
                      </div>
                    </div>
                  )}

                  {wizardStep === 5 && (
                    <div className="space-y-4">
                      <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Style Visuel & Effets</h3>
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Source d'Images</label>
                        <select 
                          value={newChannel.image_style.source}
                          onChange={e => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, source: e.target.value } })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                        >
                          <option value="library">Bibliothèque / Banques d'Images Locales</option>
                          <option value="ai_generated">Génération IA Automatique (Prompts per-segment)</option>
                        </select>
                      </div>
                    </div>
                  )}

                  <div className="flex justify-between items-center mt-8 pt-6 border-t border-surface-container-highest">
                    {wizardStep > 1 ? (
                      <button 
                        onClick={() => setWizardStep(wizardStep - 1)}
                        className="px-6 py-2 rounded-xl bg-surface-container-high text-on-surface font-label-bold hover:bg-surface-variant transition-colors"
                      >
                        Retour
                      </button>
                    ) : <div></div>}

                    {wizardStep < 5 ? (
                      <button 
                        onClick={() => setWizardStep(wizardStep + 1)}
                        className="px-6 py-2 rounded-xl bg-primary-container text-on-primary-container font-label-bold hover:bg-primary transition-colors flex items-center gap-2"
                      >
                        Suivant <span className="material-symbols-outlined text-[18px]">arrow_forward</span>
                      </button>
                    ) : (
                      <button 
                        onClick={handleCreateChannel}
                        disabled={loading}
                        className="px-6 py-2 rounded-xl bg-primary-container text-on-primary-container font-label-bold hover:bg-primary transition-colors flex items-center gap-2"
                      >
                        <span className="material-symbols-outlined text-[18px]">check</span> {loading ? "Enregistrement..." : "Créer le Pipeline"}
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* VIEW 3: CHANNEL DETAIL */}
            {view === 'channel_detail' && activeChannel && (
              <div className="space-y-8">
                <section className="flex flex-col md:flex-row md:items-end justify-between gap-6">
                  <div className="flex items-center gap-6">
                    <div className="w-20 h-20 rounded-xl bg-surface-container-high border border-outline-variant flex items-center justify-center text-primary-container font-bold text-2xl">
                      {activeChannel.name.slice(0, 2).toUpperCase()}
                    </div>
                    <div>
                      <h1 className="font-display-lg text-display-lg text-on-surface">{activeChannel.name}</h1>
                      <div className="flex items-center gap-4 text-on-surface-variant font-body-md">
                        <span>Niche: <strong>{activeChannel.niche}</strong></span>
                        <span>•</span>
                        <span className="font-mono-label text-mono-label">ID: {activeChannel.id.slice(0, 8)}...</span>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center gap-3">
                    <button 
                      onClick={() => setShowSubmitModal(true)}
                      className="px-5 py-2.5 bg-primary-container text-on-primary-container rounded-xl font-label-bold text-label-bold hover:bg-primary transition-colors flex items-center gap-2 shadow-lg shadow-primary-container/20"
                    >
                      <span className="material-symbols-outlined text-[18px]" style={{ fontVariationSettings: "'FILL' 1" }}>add</span>
                      Nouvelle Vidéo
                    </button>
                  </div>
                </section>

                <section>
                  <h3 className="font-headline-md text-headline-md text-on-surface mb-4">Vidéos de la Chaîne</h3>
                  {channelVideos.length === 0 ? (
                    <div className="bg-level-1 rounded-xl p-10 text-center">
                      <span className="material-symbols-outlined text-[40px] text-on-surface-variant mb-2">description</span>
                      <h4 className="font-title-sm text-title-sm text-on-surface mb-1">Aucune vidéo soumise</h4>
                      <p className="text-on-surface-variant mb-4">Soumettez votre premier sujet (texte de script ou fichiers audio).</p>
                      <button 
                        onClick={() => setShowSubmitModal(true)}
                        className="bg-primary-container text-on-primary-container px-5 py-2.5 rounded-xl font-label-bold hover:bg-primary transition-colors"
                      >
                        Soumettre un sujet de vidéo
                      </button>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {channelVideos.map(vid => (
                        <div key={vid.id} className="bg-level-1 rounded-xl p-5 flex justify-between items-center border border-surface-container-highest">
                          <div className="space-y-1 max-w-[70%]">
                            <div className="flex items-center gap-3">
                              <span className={`px-2.5 py-1 rounded-md text-[11px] font-mono-label font-bold uppercase ${
                                vid.status === 'done' ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' :
                                vid.status === 'rendering' ? 'bg-blue-950 text-blue-300 border border-blue-800 animate-pulse' :
                                vid.status === 'failed' ? 'bg-red-950 text-red-300 border border-red-800' :
                                'bg-yellow-950 text-yellow-300 border border-yellow-800'
                              }`}>
                                {vid.status}
                              </span>
                              <span className="text-xs text-on-surface-variant font-mono-label">
                                Mode: {vid.input_type === 'audio' ? 'Audio importé' : 'Texte Izivoice'}
                              </span>
                            </div>
                            <p className="text-on-surface text-sm line-clamp-1 italic font-semibold">
                              "{vid.script_text}"
                            </p>
                          </div>

                          <div className="flex gap-2">
                            {vid.status === 'done' && (
                              <button 
                                onClick={() => setSelectedVideo(vid)}
                                className="px-4 py-2 bg-primary-container text-on-primary-container rounded-xl font-label-bold text-xs hover:bg-primary transition-colors flex items-center gap-1.5"
                              >
                                <span className="material-symbols-outlined text-[16px]">play_circle</span> Voir Vidéo
                              </button>
                            )}
                            {vid.status === 'failed' && (
                              <button 
                                onClick={() => handleRetryVideo(vid.id)}
                                className="px-4 py-2 bg-surface-container-high text-on-surface rounded-xl font-label-bold text-xs hover:bg-surface-variant transition-colors flex items-center gap-1.5"
                              >
                                <span className="material-symbols-outlined text-[16px]">refresh</span> Relancer
                              </button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
              </div>
            )}

            {/* REFINED AUTHENTICATION MODAL */}
            {showAuthModal && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[440px] w-full border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-center mb-6">
                    <h3 className="font-headline-md text-headline-md text-on-surface">
                      {authTab === 'login' && 'Connexion'}
                      {authTab === 'register' && 'Inscription'}
                      {authTab === 'forgot' && 'Mot de passe oublié'}
                    </h3>
                    <button onClick={() => setShowAuthModal(false)} className="text-on-surface-variant hover:text-on-surface p-1">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  {resetSuccessMsg && (
                    <div className="mb-4 p-3 bg-emerald-950/80 border border-emerald-800 text-emerald-300 text-xs rounded-xl">
                      {resetSuccessMsg}
                    </div>
                  )}

                  {/* Mode Tabs */}
                  <div className="grid grid-cols-2 gap-2 bg-surface-container-lowest p-1.5 rounded-xl mb-6 border border-surface-container-highest">
                    <button 
                      onClick={() => { setAuthTab('login'); setResetSuccessMsg(''); }}
                      className={`py-2 rounded-lg text-xs font-label-bold transition-all ${authTab === 'login' ? 'bg-primary-container text-on-primary-container font-bold shadow' : 'text-on-surface-variant'}`}
                    >
                      Se connecter
                    </button>
                    <button 
                      onClick={() => { setAuthTab('register'); setResetSuccessMsg(''); }}
                      className={`py-2 rounded-lg text-xs font-label-bold transition-all ${authTab === 'register' ? 'bg-primary-container text-on-primary-container font-bold shadow' : 'text-on-surface-variant'}`}
                    >
                      S'inscrire
                    </button>
                  </div>

                  {/* Google OAuth Button */}
                  <button 
                    onClick={handleGoogleAuth}
                    type="button"
                    className="w-full py-3 mb-4 bg-surface hover:bg-surface-container-high text-on-surface border border-surface-container-highest rounded-xl text-xs font-label-bold flex items-center justify-center gap-3 transition-colors shadow-sm"
                  >
                    <svg className="w-4 h-4" viewBox="0 0 24 24">
                      <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.665-5.17 3.665-9.17z"/>
                      <path fill="#34A853" d="M12 24c3.24 0 5.95-1.08 7.93-2.91l-3.88-3.05c-1.08.72-2.45 1.16-4.05 1.16-3.12 0-5.77-2.11-6.72-4.96H1.29v3.15C3.26 21.3 7.31 24 12 24z"/>
                      <path fill="#FBBC05" d="M5.28 14.24c-.25-.72-.38-1.49-.38-2.24s.13-1.52.38-2.24V6.61H1.29C.47 8.24 0 10.06 0 12s.47 3.76 1.29 5.39l3.99-3.15z"/>
                      <path fill="#EA4335" d="M12 4.75c1.77 0 3.35.61 4.6 1.8l3.42-3.42C17.95 1.19 15.24 0 12 0 7.31 0 3.26 2.7 1.29 6.61l3.99 3.15c.95-2.85 3.6-4.96 6.72-4.96z"/>
                    </svg>
                    Continuer avec Google
                  </button>

                  <div className="flex items-center gap-3 mb-4">
                    <div className="flex-1 h-[1px] bg-surface-container-highest"></div>
                    <span className="text-[11px] text-on-surface-variant uppercase font-mono">ou email</span>
                    <div className="flex-1 h-[1px] bg-surface-container-highest"></div>
                  </div>

                  <form onSubmit={handleAuthSubmit} className="space-y-4">
                    <div>
                      <label className="block font-label-bold text-on-surface-variant mb-1 text-xs">Adresse Email</label>
                      <input 
                        type="email"
                        required
                        value={authForm.email}
                        onChange={e => setAuthForm({ ...authForm, email: e.target.value })}
                        className="w-full bg-surface border border-surface-container-highest rounded-xl p-3 text-sm text-on-surface focus:border-primary-container outline-none"
                        placeholder="nom@exemple.com"
                      />
                    </div>

                    <div>
                      <div className="flex justify-between items-center mb-1">
                        <label className="block font-label-bold text-on-surface-variant text-xs">
                          {authTab === 'forgot' ? 'Nouveau mot de passe' : 'Mot de passe'}
                        </label>
                        {authTab === 'login' && (
                          <button 
                            type="button" 
                            onClick={() => setAuthTab('forgot')}
                            className="text-[11px] text-primary-container hover:underline"
                          >
                            Mot de passe oublié ?
                          </button>
                        )}
                      </div>
                      <div className="relative">
                        <input 
                          type={showPassword ? "text" : "password"}
                          required
                          value={authForm.password}
                          onChange={e => setAuthForm({ ...authForm, password: e.target.value })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl p-3 pr-10 text-sm text-on-surface focus:border-primary-container outline-none"
                          placeholder="••••••••"
                        />
                        <button 
                          type="button"
                          onClick={() => setShowPassword(!showPassword)}
                          className="absolute right-3 top-1/2 -translate-y-1/2 text-on-surface-variant hover:text-on-surface"
                        >
                          <span className="material-symbols-outlined text-[18px]">
                            {showPassword ? 'visibility_off' : 'visibility'}
                          </span>
                        </button>
                      </div>
                    </div>

                    <button 
                      type="submit"
                      disabled={loading}
                      className="w-full py-3 bg-primary-container text-on-primary-container rounded-xl font-label-bold hover:bg-primary transition-colors flex items-center justify-center gap-2 mt-6 shadow-md"
                    >
                      <span className="material-symbols-outlined text-[18px]">lock_open</span>
                      {loading ? "Chargement..." : authTab === 'register' ? "Créer mon compte" : authTab === 'forgot' ? "Réinitialiser" : "Se connecter"}
                    </button>
                  </form>
                </div>
              </div>
            )}

            {/* REFINED PROFILE & SETTINGS MODAL (With Supabase VPS Service Status Section) */}
            {showProfileModal && currentUser && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[600px] w-full max-h-[90vh] overflow-y-auto border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-center mb-6">
                    <div className="flex items-center gap-3">
                      <div className="w-10 h-10 rounded-full bg-primary-container text-on-primary-container flex items-center justify-center font-bold text-base">
                        {currentUser.name.slice(0, 1).toUpperCase()}
                      </div>
                      <div>
                        <h3 className="font-headline-md text-headline-md text-on-surface">{currentUser.name}</h3>
                        <p className="text-xs text-on-surface-variant">{currentUser.email}</p>
                      </div>
                    </div>
                    <button onClick={() => setShowProfileModal(false)} className="text-on-surface-variant hover:text-on-surface p-1">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  {/* Profile Tabs */}
                  <div className="grid grid-cols-3 gap-2 bg-surface-container-lowest p-1.5 rounded-xl mb-6 border border-surface-container-highest">
                    <button 
                      onClick={() => setProfileTab('database')}
                      className={`py-2 rounded-lg text-xs font-label-bold flex items-center justify-center gap-1.5 transition-all ${profileTab === 'database' ? 'bg-primary-container text-on-primary-container font-bold shadow' : 'text-on-surface-variant'}`}
                    >
                      <span className="material-symbols-outlined text-[16px]">database</span>
                      Base Supabase
                    </button>
                    <button 
                      onClick={() => setProfileTab('security')}
                      className={`py-2 rounded-lg text-xs font-label-bold flex items-center justify-center gap-1.5 transition-all ${profileTab === 'security' ? 'bg-primary-container text-on-primary-container font-bold shadow' : 'text-on-surface-variant'}`}
                    >
                      <span className="material-symbols-outlined text-[16px]">shield</span>
                      Sécurité
                    </button>
                    <button 
                      onClick={() => setProfileTab('appearance')}
                      className={`py-2 rounded-lg text-xs font-label-bold flex items-center justify-center gap-1.5 transition-all ${profileTab === 'appearance' ? 'bg-primary-container text-on-primary-container font-bold shadow' : 'text-on-surface-variant'}`}
                    >
                      <span className="material-symbols-outlined text-[16px]">palette</span>
                      Apparence
                    </button>
                  </div>

                  {/* TAB 1: Base de Données Supabase Status */}
                  {profileTab === 'database' && (
                    <div className="space-y-4">
                      <div className="bg-emerald-950/40 p-5 rounded-xl border border-emerald-800/80 space-y-3">
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-2">
                            <div className="w-3 h-3 rounded-full bg-emerald-400 animate-pulse"></div>
                            <h4 className="font-title-sm text-emerald-300 font-bold">Service Supabase PostgreSQL (Actif 🟢)</h4>
                          </div>
                          <span className="px-2.5 py-0.5 rounded-full text-[10px] font-mono bg-emerald-900 text-emerald-200 border border-emerald-700">Self-Hosted VPS</span>
                        </div>

                        <div className="space-y-2 text-xs text-on-surface-variant pt-2 border-t border-emerald-900">
                          <div className="flex justify-between">
                            <span>Service :</span>
                            <strong className="text-on-surface font-mono">{dbInfo.service_name}</strong>
                          </div>
                          <div className="flex justify-between">
                            <span>Hôte Serveur VPS :</span>
                            <strong className="text-emerald-300 font-mono">{dbInfo.database_host}</strong>
                          </div>
                          <div className="flex justify-between">
                            <span>URL Supabase API :</span>
                            <strong className="text-primary-container font-mono">{dbInfo.supabase_url}</strong>
                          </div>
                          <div className="flex justify-between">
                            <span>Tables Système Initialisées :</span>
                            <strong className="text-on-surface font-mono">users, channels, videos</strong>
                          </div>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* TAB 2: Security & Password Change */}
                  {profileTab === 'security' && (
                    <div className="space-y-6">
                      <form onSubmit={handleChangePasswordSubmit} className="space-y-4 bg-surface p-4 rounded-xl border border-surface-container-highest">
                        <h4 className="font-title-sm text-on-surface font-semibold flex items-center gap-2">
                          <span className="material-symbols-outlined text-primary-container text-[18px]">key</span>
                          Changer le mot de passe
                        </h4>

                        <div>
                          <label className="block text-xs font-label-bold text-on-surface-variant mb-1">Ancien mot de passe</label>
                          <input 
                            type="password"
                            required
                            value={passwordForm.old_password}
                            onChange={e => setPasswordForm({ ...passwordForm, old_password: e.target.value })}
                            className="w-full bg-surface-container border border-surface-container-highest rounded-xl p-2.5 text-xs text-on-surface outline-none focus:border-primary-container"
                            placeholder="••••••••"
                          />
                        </div>

                        <div>
                          <label className="block text-xs font-label-bold text-on-surface-variant mb-1">Nouveau mot de passe</label>
                          <input 
                            type="password"
                            required
                            value={passwordForm.new_password}
                            onChange={e => setPasswordForm({ ...passwordForm, new_password: e.target.value })}
                            className="w-full bg-surface-container border border-surface-container-highest rounded-xl p-2.5 text-xs text-on-surface outline-none focus:border-primary-container"
                            placeholder="••••••••"
                          />
                        </div>

                        <div>
                          <label className="block text-xs font-label-bold text-on-surface-variant mb-1">Confirmer le nouveau mot de passe</label>
                          <input 
                            type="password"
                            required
                            value={passwordForm.confirm_password}
                            onChange={e => setPasswordForm({ ...passwordForm, confirm_password: e.target.value })}
                            className="w-full bg-surface-container border border-surface-container-highest rounded-xl p-2.5 text-xs text-on-surface outline-none focus:border-primary-container"
                            placeholder="••••••••"
                          />
                        </div>

                        <button 
                          type="submit"
                          disabled={loading}
                          className="px-4 py-2 bg-primary-container text-on-primary-container rounded-xl text-xs font-label-bold hover:bg-primary transition-colors flex items-center gap-1.5"
                        >
                          <span className="material-symbols-outlined text-[16px]">save</span>
                          Mettre à jour le mot de passe
                        </button>
                      </form>

                      {/* 2FA Security Switch */}
                      <div className="bg-surface p-4 rounded-xl border border-surface-container-highest flex justify-between items-center">
                        <div>
                          <h4 className="font-title-sm text-on-surface font-semibold">Authentification à deux facteurs (2FA)</h4>
                          <p className="text-xs text-on-surface-variant mt-0.5">Sécurisez votre compte avec un code d'authentification.</p>
                        </div>
                        <button 
                          onClick={() => setEnable2FA(!enable2FA)}
                          className={`w-12 h-6 rounded-full transition-colors relative p-1 ${enable2FA ? 'bg-primary-container' : 'bg-surface-container-high'}`}
                        >
                          <div className={`w-4 h-4 rounded-full bg-white transition-transform ${enable2FA ? 'translate-x-6' : 'translate-x-0'}`}></div>
                        </button>
                      </div>
                    </div>
                  )}

                  {/* TAB 3: Appearance & Theme Switch */}
                  {profileTab === 'appearance' && (
                    <div className="space-y-4">
                      <div className="bg-surface p-4 rounded-xl border border-surface-container-highest space-y-3">
                        <h4 className="font-title-sm text-on-surface font-semibold">Thème d'affichage</h4>
                        <div className="grid grid-cols-2 gap-3">
                          <button 
                            onClick={() => setThemeMode('dark')}
                            className={`p-4 rounded-xl border flex flex-col items-center gap-2 transition-all ${themeMode === 'dark' ? 'border-primary-container bg-primary-container/10 text-primary-container' : 'border-surface-container-highest text-on-surface-variant hover:bg-surface-container-high'}`}
                          >
                            <span className="material-symbols-outlined text-[28px]">dark_mode</span>
                            <span className="text-xs font-bold">Mode Sombre 🌙</span>
                          </button>
                          <button 
                            onClick={() => setThemeMode('light')}
                            className={`p-4 rounded-xl border flex flex-col items-center gap-2 transition-all ${themeMode === 'light' ? 'border-primary-container bg-primary-container/10 text-primary-container' : 'border-surface-container-highest text-on-surface-variant hover:bg-surface-container-high'}`}
                          >
                            <span className="material-symbols-outlined text-[28px]">light_mode</span>
                            <span className="text-xs font-bold">Mode Clair ☀️</span>
                          </button>
                        </div>
                      </div>
                    </div>
                  )}

                  {/* Logout Button */}
                  <div className="mt-8 pt-4 border-t border-surface-container-highest flex justify-end">
                    <button 
                      onClick={handleLogout}
                      className="px-5 py-2.5 bg-red-950/80 hover:bg-red-900 text-red-300 border border-red-800 rounded-xl text-xs font-label-bold flex items-center gap-2 transition-colors"
                    >
                      <span className="material-symbols-outlined text-[16px]">logout</span>
                      Déconnexion du compte
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* VIDEO SUBMISSION MODAL */}
            {showSubmitModal && activeChannel && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[760px] w-full max-h-[90vh] overflow-y-auto border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-center mb-6">
                    <div>
                      <h3 className="font-headline-md text-headline-md text-on-surface">Soumettre un sujet de vidéo</h3>
                      <p className="text-on-surface-variant text-sm mt-1">Chaîne : <strong className="text-primary-container">{activeChannel.name}</strong></p>
                    </div>
                    <button onClick={() => setShowSubmitModal(false)} className="text-on-surface-variant hover:text-on-surface p-1">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  <div className="grid grid-cols-2 gap-3 bg-surface-container-lowest p-2 rounded-xl mb-6 border border-surface-container-highest">
                    <button 
                      onClick={() => setSubmitMode('text')}
                      className={`py-3 rounded-lg text-sm font-label-bold flex items-center justify-center gap-2 transition-all ${submitMode === 'text' ? 'bg-primary-container text-on-primary-container font-bold shadow-md' : 'text-on-surface-variant hover:bg-surface-container-high'}`}
                    >
                      <span className="material-symbols-outlined text-[18px]">description</span>
                      Script Texte
                    </button>

                    <button 
                      onClick={() => setSubmitMode('audio_upload')}
                      className={`py-3 rounded-lg text-sm font-label-bold flex items-center justify-center gap-2 transition-all ${submitMode === 'audio_upload' ? 'bg-primary-container text-on-primary-container font-bold shadow-md' : 'text-on-surface-variant hover:bg-surface-container-high'}`}
                    >
                      <span className="material-symbols-outlined text-[18px]">cloud_upload</span>
                      Audio déjà prêt
                    </button>
                  </div>

                  {submitMode === 'text' && (
                    <div className="space-y-4">
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-2">Texte du script de la vidéo</label>
                        <textarea
                          value={singleScriptText}
                          onChange={e => setSingleScriptText(e.target.value)}
                          rows={12}
                          style={{ minHeight: '280px' }}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl p-4 text-sm text-on-surface focus:border-primary-container outline-none resize-y"
                          placeholder="Collez ici le script texte complet de votre vidéo. L'API d'Izivoice générera la voix-off et les sous-titres..."
                        />
                      </div>
                      <div className="flex justify-end pt-2">
                        <button 
                          onClick={handleSubjectSubmit}
                          disabled={loading}
                          className="px-6 py-3 bg-primary-container text-on-primary-container rounded-xl font-label-bold hover:bg-primary transition-colors flex items-center gap-2 shadow-md"
                        >
                          <span className="material-symbols-outlined">rocket_launch</span>
                          {loading ? "Traitement..." : "Générer la vidéo via Izivoice"}
                        </button>
                      </div>
                    </div>
                  )}

                  {submitMode === 'audio_upload' && (
                    <div className="space-y-4">
                      <div 
                        onDragOver={handleDragOver}
                        onDragLeave={handleDragLeave}
                        onDrop={handleDrop}
                        onClick={() => fileInputRef.current && fileInputRef.current.click()}
                        className={`w-full p-8 rounded-xl border-2 border-dashed transition-all text-center cursor-pointer flex flex-col items-center justify-center gap-3 ${
                          isDragging ? 'border-primary-container bg-primary-container/10' : 'border-surface-container-highest bg-surface hover:border-outline'
                        }`}
                      >
                        <input 
                          type="file" 
                          ref={fileInputRef}
                          multiple
                          accept="audio/*,video/*,.mp3,.wav,.m4a,.mp4,.aac,.flac,.ogg"
                          onChange={handleFileSelect}
                          className="hidden"
                        />
                        <div className="w-14 h-14 rounded-full bg-surface-container-high flex items-center justify-center text-primary-container">
                          <span className="material-symbols-outlined text-[32px]">cloud_upload</span>
                        </div>
                        <div>
                          <p className="font-title-sm text-on-surface font-semibold">Glissez-déposez vos fichiers audio ici</p>
                          <p className="text-xs text-on-surface-variant mt-1">Formats acceptés : MP3, WAV, M4A, MP4, AAC, FLAC, OGG</p>
                          <p className="text-xs text-primary-container font-mono mt-1">Vous pouvez sélectionner un ou plusieurs fichiers à la fois</p>
                        </div>
                      </div>

                      {audioFilesList.length > 0 && (
                        <div className="space-y-2 max-h-[180px] overflow-y-auto bg-surface-container-lowest p-3 rounded-xl border border-surface-container-highest">
                          <div className="text-xs font-label-bold text-on-surface-variant mb-2">Fichiers audio sélectionnés ({audioFilesList.length}) :</div>
                          {audioFilesList.map((file, idx) => (
                            <div key={idx} className="flex justify-between items-center bg-surface-container p-2.5 rounded-lg border border-surface-container-high text-xs">
                              <div className="flex items-center gap-2 truncate max-w-[85%]">
                                <span className="material-symbols-outlined text-primary-container text-[16px]">audiotrack</span>
                                <span className="text-on-surface font-mono truncate">{file.name}</span>
                                <span className="text-on-surface-variant text-[10px]">({(file.size / (1024 * 1024)).toFixed(2)} MB)</span>
                              </div>
                              <button 
                                onClick={(e) => { e.stopPropagation(); removeAudioFile(idx); }}
                                className="text-error hover:text-red-400 p-1"
                              >
                                <span className="material-symbols-outlined text-[16px]">close</span>
                              </button>
                            </div>
                          ))}
                        </div>
                      )}

                      <div className="flex justify-end pt-2">
                        <button 
                          onClick={handleSubjectSubmit}
                          disabled={loading || audioFilesList.length === 0}
                          className="px-6 py-3 bg-primary-container text-on-primary-container rounded-xl font-label-bold hover:bg-primary transition-colors flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed shadow-md"
                        >
                          <span className="material-symbols-outlined">movie_filter</span>
                          {loading ? "Téléversement..." : `Lancer le montage des audios (${audioFilesList.length} fichier(s))`}
                        </button>
                      </div>
                    </div>
                  )}

                </div>
              </div>
            )}

            {/* VIDEO RESULT MODAL */}
            {selectedVideo && (
              <div className="fixed inset-0 bg-black/80 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[800px] w-full max-h-[90vh] overflow-y-auto border border-surface-container-highest">
                  <div className="flex justify-between items-center mb-4">
                    <h3 className="font-headline-md text-headline-md text-on-surface">Lecteur Vidéo MP4</h3>
                    <button onClick={() => setSelectedVideo(null)} className="text-on-surface-variant hover:text-on-surface">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  <div className="rounded-xl overflow-hidden bg-black mb-6">
                    <video 
                      controls 
                      autoPlay 
                      className="w-full max-h-[420px]" 
                      src={`${STORAGE_BASE}/${selectedVideo.output_path?.replace('storage/', '')}`} 
                    />
                  </div>

                  <div className="grid grid-cols-2 gap-4">
                    <div className="bg-surface p-4 rounded-xl border border-surface-container-highest">
                      <div className="font-label-bold text-on-surface-variant mb-1">Dossier Source Archive</div>
                      <div className="font-mono text-xs text-on-surface truncate">{selectedVideo.source_assets_path}</div>
                    </div>

                    <div className="bg-surface p-4 rounded-xl border border-surface-container-highest flex items-center justify-center">
                      <a 
                        href={`${STORAGE_BASE}/${selectedVideo.output_path?.replace('storage/', '')}`} 
                        download 
                        className="bg-primary-container text-on-primary-container px-4 py-2 rounded-xl font-label-bold text-sm hover:bg-primary transition-colors inline-flex items-center gap-2"
                      >
                        <span className="material-symbols-outlined">download</span> Télécharger MP4
                      </a>
                    </div>
                  </div>
                </div>
              </div>
            )}

          </div>
        </div>
      </main>
    </div>
  );
}
