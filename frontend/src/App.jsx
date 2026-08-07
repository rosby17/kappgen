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
  const [showChannelPickerModal, setShowChannelPickerModal] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // User Auth State
  const [currentUser, setCurrentUser] = useState(() => {
    const saved = localStorage.getItem("nichecut_user");
    return saved ? JSON.parse(saved) : null;
  });
  const [authTab, setAuthTab] = useState('login'); // 'login' | 'register'
  const [authForm, setAuthForm] = useState({ email: '', name: '', password: '' });

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
    if (!authForm.email || !authForm.password) return alert("Veuillez remplir tous les champs obligatoires.");

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
        setAuthForm({ email: '', name: '', password: '' });
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

  const handleLogout = () => {
    setCurrentUser(null);
    localStorage.removeItem("nichecut_user");
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

        <div className="px-4 mt-auto space-y-4">
          {currentUser ? (
            <div className="p-3 bg-surface-container-low rounded-xl border border-surface-container-highest space-y-2">
              <div className="flex items-center gap-2 truncate">
                <span className="material-symbols-outlined text-primary-container">account_circle</span>
                <span className="text-xs text-on-surface font-semibold truncate">{currentUser.name}</span>
              </div>
              <button 
                onClick={handleLogout}
                className="w-full text-xs text-error hover:underline text-left font-mono"
              >
                Déconnexion
              </button>
            </div>
          ) : (
            <button 
              onClick={() => setShowAuthModal(true)}
              className="w-full py-2 bg-primary-container text-on-primary-container rounded-xl font-label-bold text-label-bold hover:bg-primary transition-colors flex items-center justify-center gap-2"
            >
              <span className="material-symbols-outlined text-[18px]">lock</span>
              Se connecter
            </button>
          )}

          <div className="space-y-1">
            <div className="flex items-center gap-3 px-4 py-2 text-on-surface-variant font-body-sm">
              <span className="material-symbols-outlined text-[18px]">database</span>
              Supabase / DB Ready
            </div>
          </div>
        </div>
      </nav>

      {/* Main Content Area */}
      <main className="flex-1 flex flex-col md:ml-[240px] h-screen overflow-hidden bg-background">
        
        {/* Top Header */}
        <div className="hidden md:flex justify-between items-center px-xl py-6 border-b border-outline-variant">
          <h1 className="font-display-lg text-display-lg text-on-surface">
            {view === 'dashboard' && 'Dashboard'}
            {view === 'wizard' && (wizardMode === 'edit' ? 'Modifier la Chaîne' : 'Assistant de Création')}
            {view === 'channel_detail' && (activeChannel ? activeChannel.name : 'Détail Chaîne')}
          </h1>
          <div className="flex items-center gap-4">
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

            {/* Profile Header Button */}
            {currentUser ? (
              <div className="flex items-center gap-2 bg-surface-container-high px-3 py-1.5 rounded-xl border border-surface-container-highest">
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
                      {filteredChannels.map(chan => {
                        const logoUrl = getChannelLogoUrl(chan);
                        const statusInfo = getChannelStatusInfo(chan);
                        return (
                        <div
                          key={chan.id}
                          onClick={() => { setActiveChannel(chan); fetchChannelVideos(chan.id); setView('channel_detail'); }}
                          className="bg-level-1 rounded-xl p-5 hover:bg-surface-container-high transition-colors border border-transparent hover:border-outline-variant cursor-pointer group flex flex-col justify-between min-h-[190px]"
                        >
                          <div className="flex items-start justify-between">
                            <div className="flex items-center gap-3 min-w-0">
                              {logoUrl ? (
                                <img src={logoUrl} alt={chan.name} className="w-10 h-10 rounded-xl object-cover border border-surface-container-highest flex-shrink-0" />
                              ) : (
                                <div className="w-10 h-10 rounded-xl bg-[#004c66] text-[#c2e8ff] flex items-center justify-center font-bold font-title-sm flex-shrink-0">
                                  {chan.name.slice(0, 2).toUpperCase()}
                                </div>
                              )}
                              <div className="min-w-0">
                                <h4 className="font-title-sm text-title-sm text-on-surface group-hover:text-primary-container transition-colors truncate">{chan.name}</h4>
                                <span className="font-label-bold text-label-bold text-on-surface-variant truncate block">{chan.niche}</span>
                              </div>
                            </div>
                            <div className="flex items-center gap-1 flex-shrink-0">
                              <button
                                onClick={(e) => openEditWizard(chan, e)}
                                className="text-on-surface-variant hover:text-primary-container p-1"
                                title="Modifier la chaîne"
                              >
                                <span className="material-symbols-outlined text-[18px]">edit</span>
                              </button>
                              <button
                                onClick={(e) => handleDeleteChannel(chan.id, e)}
                                className="text-error hover:text-red-400 p-1"
                                title="Supprimer la chaîne"
                              >
                                <span className="material-symbols-outlined text-[18px]">delete</span>
                              </button>
                            </div>
                          </div>

                          <span className={`self-start mt-3 px-2.5 py-1 rounded-md text-[11px] font-mono-label font-bold uppercase ${statusInfo.className}`}>
                            {statusInfo.label}
                          </span>

                          <div className="grid grid-cols-2 gap-2 mt-3">
                            <div className="bg-surface p-2 rounded-xl border border-surface-container-highest">
                              <div className="font-label-bold text-label-bold text-on-surface-variant mb-1">En File</div>
                              <div className="font-body-md text-body-md text-primary-container font-bold">{(chan.queued_count || 0) + (chan.rendering_count || 0)}</div>
                            </div>
                            <div className="bg-surface p-2 rounded-xl border border-surface-container-highest">
                              <div className="font-label-bold text-label-bold text-on-surface-variant mb-1">Vidéos Prêtes</div>
                              <div className="font-body-md text-body-md text-on-surface font-bold">{chan.done_count || 0}</div>
                            </div>
                          </div>

                          <button
                            onClick={(e) => { e.stopPropagation(); setActiveChannel(chan); setShowSubmitModal(true); }}
                            className="mt-3 w-full py-2 bg-surface-container-high text-on-surface rounded-xl font-label-bold text-xs hover:bg-primary-container hover:text-on-primary-container transition-colors flex items-center justify-center gap-1.5"
                          >
                            <span className="material-symbols-outlined text-[16px]">add</span> Nouvelle Vidéo
                          </button>
                        </div>
                        );
                      })}

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
                  onClick={() => { setView(wizardMode === 'edit' ? 'channel_detail' : 'dashboard'); resetWizardState(); }}
                  className="text-on-surface-variant hover:text-on-surface flex items-center gap-2 font-label-bold"
                >
                  <span className="material-symbols-outlined">arrow_back</span> Retour
                </button>

                <div className="bg-level-1 rounded-xl p-8 border border-surface-container-highest">
                  <h2 className="font-display-lg text-display-lg text-on-surface mb-2">{wizardMode === 'edit' ? 'Modifier la Chaîne' : 'Assistant de Création'}</h2>
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
                        <label className="block font-label-bold text-on-surface-variant mb-2">Photo / logo de la chaîne</label>
                        <div className="flex items-center gap-4">
                          <div
                            onClick={() => logoInputRef.current && logoInputRef.current.click()}
                            className="w-20 h-20 rounded-xl bg-surface border border-surface-container-highest hover:border-primary-container cursor-pointer flex items-center justify-center overflow-hidden flex-shrink-0 transition-colors"
                          >
                            {logoPreviewUrl ? (
                              <img src={logoPreviewUrl} alt="Logo" className="w-full h-full object-cover" />
                            ) : (
                              <span className="material-symbols-outlined text-on-surface-variant text-[28px]">add_a_photo</span>
                            )}
                          </div>
                          <div>
                            <input
                              type="file"
                              ref={logoInputRef}
                              accept="image/png,image/jpeg,image/webp,image/gif,image/svg+xml"
                              onChange={handleLogoFileSelect}
                              className="hidden"
                            />
                            <button
                              type="button"
                              onClick={() => logoInputRef.current && logoInputRef.current.click()}
                              className="px-4 py-2 bg-surface-container-high text-on-surface rounded-xl font-label-bold text-xs hover:bg-surface-variant transition-colors"
                            >
                              {logoPreviewUrl ? "Changer l'image" : "Choisir une image"}
                            </button>
                            <p className="text-xs text-on-surface-variant mt-1">PNG, JPG, WEBP, GIF ou SVG</p>
                          </div>
                        </div>
                      </div>

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
                          <option value="library">Dossier Local (mes propres images)</option>
                          <option value="ai_generated">Génération IA Automatique (payant, par segment)</option>
                        </select>
                      </div>

                      {newChannel.image_style.source === 'library' ? (
                        <div>
                          <label className="block font-label-bold text-on-surface-variant mb-2">Chemin du dossier d'images local</label>
                          <input
                            value={newChannel.image_style.library_path || ''}
                            onChange={e => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, library_path: e.target.value } })}
                            className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none font-mono text-sm"
                            placeholder="/Users/moi/Images/ma-chaine"
                          />
                          <p className="text-xs text-on-surface-variant mt-2">Chemin absolu, sur la machine qui exécute NicheCut, vers un dossier contenant vos images (JPG, PNG, WEBP). Laissez vide pour utiliser la bibliothèque par défaut.</p>
                        </div>
                      ) : (
                        <div>
                          <label className="block font-label-bold text-on-surface-variant mb-2">Style / ambiance des images générées</label>
                          <input
                            value={newChannel.image_style.style_prompt || ''}
                            onChange={e => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, style_prompt: e.target.value } })}
                            className="w-full bg-surface border border-surface-container-highest rounded-xl px-4 py-3 text-on-surface focus:border-primary-container outline-none"
                            placeholder="cinematic dramatic lighting, high detail"
                          />
                          <p className="text-xs text-on-surface-variant mt-2">Chaque image de la vidéo est générée automatiquement via IA (ai33.pro) — des crédits sont consommés à chaque vidéo générée.</p>
                        </div>
                      )}
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
                        onClick={handleSaveChannel}
                        disabled={loading}
                        className="px-6 py-2 rounded-xl bg-primary-container text-on-primary-container font-label-bold hover:bg-primary transition-colors flex items-center gap-2"
                      >
                        <span className="material-symbols-outlined text-[18px]">check</span>
                        {loading ? "Enregistrement..." : (wizardMode === 'edit' ? "Enregistrer les modifications" : "Créer le Pipeline")}
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
                  <div className="flex items-center gap-6 min-w-0">
                    {getChannelLogoUrl(activeChannel) ? (
                      <img src={getChannelLogoUrl(activeChannel)} alt={activeChannel.name} className="w-20 h-20 rounded-xl object-cover border border-outline-variant flex-shrink-0" />
                    ) : (
                      <div className="w-20 h-20 rounded-xl bg-surface-container-high border border-outline-variant flex items-center justify-center text-primary-container font-bold text-2xl flex-shrink-0">
                        {activeChannel.name.slice(0, 2).toUpperCase()}
                      </div>
                    )}
                    <div className="min-w-0">
                      <h1 className="font-display-lg text-display-lg text-on-surface truncate">{activeChannel.name}</h1>
                      <div className="flex items-center gap-4 text-on-surface-variant font-body-md">
                        <span>Niche: <strong>{activeChannel.niche}</strong></span>
                        <span>•</span>
                        <span className="font-mono-label text-mono-label">ID: {activeChannel.id.slice(0, 8)}...</span>
                      </div>
                      {(() => {
                        const s = getChannelStatusInfo(activeChannel);
                        return <span className={`inline-block mt-2 px-2.5 py-1 rounded-md text-[11px] font-mono-label font-bold uppercase ${s.className}`}>{s.label}</span>;
                      })()}
                    </div>
                  </div>

                  <div className="flex items-center gap-3 flex-shrink-0">
                    <button
                      onClick={(e) => openEditWizard(activeChannel, e)}
                      className="px-4 py-2.5 bg-surface-container-high text-on-surface rounded-xl font-label-bold text-label-bold hover:bg-surface-variant transition-colors flex items-center gap-2"
                    >
                      <span className="material-symbols-outlined text-[18px]">edit</span>
                      Modifier
                    </button>
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

            {/* USER AUTHENTICATION MODAL */}
            {showAuthModal && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[480px] w-full border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-center mb-6">
                    <h3 className="font-headline-md text-headline-md text-on-surface">Compte Utilisateur</h3>
                    <button onClick={() => setShowAuthModal(false)} className="text-on-surface-variant hover:text-on-surface">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  <div className="grid grid-cols-2 gap-2 bg-surface-container-lowest p-1.5 rounded-xl mb-6">
                    <button 
                      onClick={() => setAuthTab('login')}
                      className={`py-2 rounded-lg text-xs font-label-bold transition-all ${authTab === 'login' ? 'bg-primary-container text-on-primary-container font-bold' : 'text-on-surface-variant'}`}
                    >
                      Se Connecter
                    </button>
                    <button 
                      onClick={() => setAuthTab('register')}
                      className={`py-2 rounded-lg text-xs font-label-bold transition-all ${authTab === 'register' ? 'bg-primary-container text-on-primary-container font-bold' : 'text-on-surface-variant'}`}
                    >
                      Créer un Compte
                    </button>
                  </div>

                  <form onSubmit={handleAuthSubmit} className="space-y-4">
                    {authTab === 'register' && (
                      <div>
                        <label className="block font-label-bold text-on-surface-variant mb-1 text-xs">Nom complet</label>
                        <input 
                          value={authForm.name}
                          onChange={e => setAuthForm({ ...authForm, name: e.target.value })}
                          className="w-full bg-surface border border-surface-container-highest rounded-xl p-3 text-sm text-on-surface focus:border-primary-container outline-none"
                          placeholder="Ex: Jean Dupont"
                        />
                      </div>
                    )}

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
                      <label className="block font-label-bold text-on-surface-variant mb-1 text-xs">Mot de passe</label>
                      <input 
                        type="password"
                        required
                        value={authForm.password}
                        onChange={e => setAuthForm({ ...authForm, password: e.target.value })}
                        className="w-full bg-surface border border-surface-container-highest rounded-xl p-3 text-sm text-on-surface focus:border-primary-container outline-none"
                        placeholder="••••••••"
                      />
                    </div>

                    <button 
                      type="submit"
                      disabled={loading}
                      className="w-full py-3 bg-primary-container text-on-primary-container rounded-xl font-label-bold hover:bg-primary transition-colors flex items-center justify-center gap-2 mt-6"
                    >
                      <span className="material-symbols-outlined text-[18px]">check</span>
                      {loading ? "Chargement..." : authTab === 'register' ? "Créer mon compte" : "Se connecter"}
                    </button>
                  </form>
                </div>
              </div>
            )}

            {/* CHANNEL PICKER MODAL — used by "Nouvelle Vidéo" when no channel context is implied */}
            {showChannelPickerModal && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[720px] w-full max-h-[85vh] overflow-y-auto border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-center mb-6">
                    <div>
                      <h3 className="font-headline-md text-headline-md text-on-surface">Choisir une chaîne</h3>
                      <p className="text-on-surface-variant text-sm mt-1">Sur quelle chaîne voulez-vous générer cette vidéo ?</p>
                    </div>
                    <button onClick={() => setShowChannelPickerModal(false)} className="text-on-surface-variant hover:text-on-surface p-1">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    {channels.map(chan => {
                      const logoUrl = getChannelLogoUrl(chan);
                      return (
                        <button
                          key={chan.id}
                          onClick={() => {
                            setActiveChannel(chan);
                            fetchChannelVideos(chan.id);
                            setShowChannelPickerModal(false);
                            setShowSubmitModal(true);
                          }}
                          className="flex items-center gap-3 bg-surface p-4 rounded-xl border border-surface-container-highest hover:border-primary-container hover:bg-surface-container-high transition-colors text-left"
                        >
                          {logoUrl ? (
                            <img src={logoUrl} alt={chan.name} className="w-12 h-12 rounded-xl object-cover border border-surface-container-highest flex-shrink-0" />
                          ) : (
                            <div className="w-12 h-12 rounded-xl bg-[#004c66] text-[#c2e8ff] flex items-center justify-center font-bold flex-shrink-0">
                              {chan.name.slice(0, 2).toUpperCase()}
                            </div>
                          )}
                          <div className="min-w-0">
                            <div className="font-title-sm text-title-sm text-on-surface truncate">{chan.name}</div>
                            <div className="font-label-bold text-label-bold text-on-surface-variant truncate">{chan.niche}</div>
                          </div>
                        </button>
                      );
                    })}

                    <button
                      onClick={() => { setShowChannelPickerModal(false); openCreateWizard(); }}
                      className="flex items-center justify-center gap-2 border-2 border-dashed border-surface-container-highest hover:border-primary-container hover:bg-surface rounded-xl p-4 text-on-surface-variant hover:text-primary-container transition-all"
                    >
                      <span className="material-symbols-outlined">add</span>
                      <span className="font-label-bold text-label-bold">Nouvelle chaîne</span>
                    </button>
                  </div>
                </div>
              </div>
            )}

            {/* VIDEO SUBMISSION MODAL */}
            {showSubmitModal && activeChannel && (
              <div className="fixed inset-0 bg-black/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
                <div className="bg-level-1 rounded-xl p-8 max-w-[760px] w-full max-h-[90vh] overflow-y-auto border border-surface-container-highest shadow-2xl">
                  <div className="flex justify-between items-start mb-6">
                    <h3 className="font-headline-md text-headline-md text-on-surface">Nouvelle vidéo</h3>
                    <button onClick={() => setShowSubmitModal(false)} className="text-on-surface-variant hover:text-on-surface p-1">
                      <span className="material-symbols-outlined">close</span>
                    </button>
                  </div>

                  <div className="flex items-center gap-4 bg-surface-container-lowest p-4 rounded-xl border border-surface-container-highest mb-6">
                    {getChannelLogoUrl(activeChannel) ? (
                      <img src={getChannelLogoUrl(activeChannel)} alt={activeChannel.name} className="w-14 h-14 rounded-xl object-cover border border-surface-container-highest flex-shrink-0" />
                    ) : (
                      <div className="w-14 h-14 rounded-xl bg-[#004c66] text-[#c2e8ff] flex items-center justify-center font-bold text-lg flex-shrink-0">
                        {activeChannel.name.slice(0, 2).toUpperCase()}
                      </div>
                    )}
                    <div className="min-w-0 flex-1">
                      <div className="font-title-sm text-title-sm text-on-surface truncate">{activeChannel.name}</div>
                      <div className="font-label-bold text-label-bold text-on-surface-variant truncate">{activeChannel.niche}</div>
                    </div>
                    {channels.length > 1 && (
                      <button
                        onClick={() => { setShowSubmitModal(false); setShowChannelPickerModal(true); }}
                        className="px-3 py-1.5 bg-surface-container-high text-on-surface-variant hover:text-on-surface rounded-lg text-xs font-label-bold flex-shrink-0 transition-colors"
                      >
                        Changer
                      </button>
                    )}
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
