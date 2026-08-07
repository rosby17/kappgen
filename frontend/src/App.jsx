import React, { useState, useEffect, useRef } from 'react';

const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000/api";
const STORAGE_BASE = import.meta.env.VITE_STORAGE_BASE || "http://localhost:8000/storage";

// Preset Subtitle Styles
const SUBTITLE_PRESETS = [
  {
    id: 'hormozi',
    name: 'Hormozi Gold 🔥',
    font: 'Montserrat',
    size: 46,
    color: '#FFD700',
    outline_color: '#000000',
    outline_width: 4,
    position: 'bottom',
    karaoke: true,
    box_color: 'transparent'
  },
  {
    id: 'tiktok_glow',
    name: 'TikTok Neon Cyan ⚡',
    font: 'Inter',
    size: 44,
    color: '#00FFFF',
    outline_color: '#003b46',
    outline_width: 3,
    position: 'bottom',
    karaoke: true,
    box_color: 'rgba(0,0,0,0.6)'
  },
  {
    id: 'cinematic_dark',
    name: 'Cinématique Épuré 🎬',
    font: 'Montserrat',
    size: 40,
    color: '#FFFFFF',
    outline_color: '#111111',
    outline_width: 2,
    position: 'bottom',
    karaoke: true,
    box_color: 'transparent'
  },
  {
    id: 'classic_stoic',
    name: 'Stoïcien Vintage 📜',
    font: 'Bebas Neue',
    size: 50,
    color: '#F5EBE0',
    outline_color: '#2B1E16',
    outline_width: 3,
    position: 'bottom',
    karaoke: false,
    box_color: 'rgba(20,15,10,0.7)'
  }
];

// Available Voice Models
const VOICE_MODELS = [
  { id: 'fr-FR-Thomas', name: 'Thomas — Voix Stoïque & Profonde', lang: 'fr-FR', desc: 'Idéal pour philosophie, citations et stoïcisme' },
  { id: 'fr-FR-Elodie', name: 'Élodie — Narrative Éléganter', lang: 'fr-FR', desc: 'Idéal pour récits historiques et contes' },
  { id: 'fr-FR-Nicolas', name: 'Nicolas — Voix Grave & Envoûtante', lang: 'fr-FR', desc: 'Idéal pour spiritualité et méditations guidées' },
  { id: 'fr-FR-Claire', name: 'Claire — Douce & Inspirante', lang: 'fr-FR', desc: 'Idéal pour développement personnel' }
];

export default function App() {
  const [channels, setChannels] = useState([]);
  const [activeChannel, setActiveChannel] = useState(null);
  const [channelVideos, setChannelVideos] = useState([]);
  const [allVideos, setAllVideos] = useState([]);
  const [view, setView] = useState('home'); // 'home', 'channels', 'videos', 'channel_detail', 'wizard'
  const [selectedVideo, setSelectedVideo] = useState(null);
  
  // Modals & Menu Popups
  const [showSubmitModal, setShowSubmitModal] = useState(false);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [showProfileModal, setShowProfileModal] = useState(false);
  const [showChannelPickerModal, setShowChannelPickerModal] = useState(false);
  const [openChannelMenuId, setOpenChannelMenuId] = useState(null);

  const [loading, setLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [videoFilterChannelId, setVideoFilterChannelId] = useState('all');
  const [toast, setToast] = useState(null); // { message, type: 'success' | 'error' }

  const showToast = (message, type = 'success') => {
    setToast({ message, type });
  };

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  // Karaoke Animation Preview Index
  const [previewWordIndex, setPreviewWordIndex] = useState(0);

  // Karaoke timer animation effect for subtitle preview
  useEffect(() => {
    const timer = setInterval(() => {
      setPreviewWordIndex(prev => (prev + 1) % 6);
    }, 800);
    return () => clearInterval(timer);
  }, []);

  // Close channel popup menus when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (!e.target.closest('.channel-menu-container')) {
        setOpenChannelMenuId(null);
      }
    };
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, []);

  // User Auth State
  const [currentUser, setCurrentUser] = useState(() => {
    const saved = localStorage.getItem("nichecut_user");
    return saved ? JSON.parse(saved) : { name: 'Mogo', email: 'rooseveltmkng@gmail.com', id: 'user-demo-1' };
  });
  const [authTab, setAuthTab] = useState('login');
  const [authForm, setAuthForm] = useState({ email: '', password: '' });
  const [showPassword, setShowPassword] = useState(false);
  const [resetSuccessMsg, setResetSuccessMsg] = useState('');


  // Submission Form State (Nouvelle Vidéo)
  const [submitMode, setSubmitMode] = useState('text'); // 'text' | 'audio_upload'
  const [singleScriptText, setSingleScriptText] = useState('');
  const [selectedVoice, setSelectedVoice] = useState('fr-FR-Thomas');
  const [audioFilesList, setAudioFilesList] = useState([]);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef(null);

  // Wizard State
  const [wizardStep, setWizardStep] = useState(1);
  const [wizardMode, setWizardMode] = useState('create');
  const [editingChannelId, setEditingChannelId] = useState(null);
  const [logoFile, setLogoFile] = useState(null);
  const [logoPreviewUrl, setLogoPreviewUrl] = useState(null);
  const logoInputRef = useRef(null);

  // Local Image Folder Upload State for Wizard Step 5
  const [localImageFiles, setLocalImageFiles] = useState([]);
  const [selectedFolderName, setSelectedFolderName] = useState('');
  const [isFolderDragging, setIsFolderDragging] = useState(false);
  const wizardFolderInputRef = useRef(null);

  const defaultChannelForm = {
    name: '',
    niche: 'Philosophie & Stoïcisme',
    subtitle_style: {
      font: 'Montserrat',
      size: 44,
      color: '#FFD700',
      outline_color: '#000000',
      outline_width: 3,
      position: 'bottom',
      karaoke: true,
      box_color: 'transparent'
    },
    branding: {
      channel_name_text: '',
      logo_path: '',
      watermark_position: 'top_right',
      watermark_opacity: 0.85
    },
    music_preference: {
      enabled: true,
      track_id_or_style: 'ambient',
      volume: 0.15
    },
    image_style: {
      source: 'library',
      style_prompt: 'cinematic dramatic lighting, high detail, stoic sculpture style, dark moody atmosphere',
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

  const fetchAllVideos = async () => {
    try {
      const res = await fetch(`${API_BASE}/videos`);
      if (res.ok) {
        const data = await res.json();
        setAllVideos(data);
      }
    } catch (e) {
      // Fallback: aggregate from channel videos if route not ready
      console.log("Fetching channel videos fallback");
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
    fetchAllVideos();
    const interval = setInterval(() => {
      fetchChannels();
      if (activeChannel) {
        fetchChannelVideos(activeChannel.id);
      }
    }, 6000);
    return () => clearInterval(interval);
  }, [activeChannel]);

  const handleAuthSubmit = async (e) => {
    e.preventDefault();
    try {
      setLoading(true);
      const endpoint = authTab === 'register' ? `${API_BASE}/auth/register` : `${API_BASE}/auth/login`;
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(authForm)
      });
      if (res.ok) {
        const data = await res.json();
        setCurrentUser(data.user);
        localStorage.setItem("nichecut_user", JSON.stringify(data.user));
        setShowAuthModal(false);
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
    setShowProfileModal(false);
  };

  const resetWizardState = () => {
    setNewChannel(defaultChannelForm);
    setWizardMode('create');
    setEditingChannelId(null);
    setLogoFile(null);
    setLogoPreviewUrl(null);
    setLocalImageFiles([]);
    setWizardStep(1);
  };

  const openCreateWizard = () => {
    resetWizardState();
    setView('wizard');
  };

  const openEditWizard = (channel, e) => {
    if (e) e.stopPropagation();
    setOpenChannelMenuId(null);
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

  const handleLocalFolderSelect = (e) => {
    const files = Array.from(e.target.files).filter(f => 
      f.type.startsWith('image/') || /\.(jpg|jpeg|png|webp|gif|svg|avif)$/i.test(f.name)
    );
    if (files.length > 0) {
      // Extract directory name from webkitRelativePath
      const firstPath = files[0].webkitRelativePath || '';
      const folderName = firstPath ? firstPath.split('/')[0] : 'Dossier Images';
      setSelectedFolderName(folderName);
      setLocalImageFiles(files);
      setNewChannel(prev => ({
        ...prev,
        image_style: {
          ...prev.image_style,
          library_path: `${folderName} (${files.length} images)`
        }
      }));
    }
  };

  const handleFolderDrop = (e) => {
    e.preventDefault();
    e.stopPropagation();
    setIsFolderDragging(false);

    const droppedFiles = Array.from(e.dataTransfer.files).filter(f => 
      f.type.startsWith('image/') || /\.(jpg|jpeg|png|webp|gif|svg|avif)$/i.test(f.name)
    );

    if (droppedFiles.length > 0) {
      const folderName = "Dossier Images Déposé";
      setSelectedFolderName(folderName);
      setLocalImageFiles(droppedFiles);
      setNewChannel(prev => ({
        ...prev,
        image_style: {
          ...prev.image_style,
          library_path: `${folderName} (${droppedFiles.length} images)`
        }
      }));
    }
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
    if (rendering > 0) return { label: 'Rendu en cours', className: 'bg-blue-950/80 text-blue-300 border border-blue-700/60 animate-pulse' };
    if (queued > 0) return { label: 'En file', className: 'bg-amber-950/80 text-amber-300 border border-amber-700/60' };
    if (done > 0) return { label: 'Prête', className: 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/60' };
    if (failed > 0) return { label: 'Échec de rendu', className: 'bg-rose-950/80 text-rose-300 border border-rose-700/60' };
    return { label: 'Configurée', className: 'bg-slate-800/80 text-slate-300 border border-slate-700/60' };
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

  const handleSubjectSubmit = async () => {
    if (!activeChannel) return alert("Veuillez sélectionner une chaîne.");

    const formData = new FormData();
    formData.append("channel_id", activeChannel.id);
    formData.append("input_type", submitMode === 'audio_upload' ? 'audio' : 'text');
    formData.append("voice_id", selectedVoice);

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
        fetchAllVideos();
        showToast("Vidéo soumise avec succès — le montage et le rendu sont lancés.", "success");
      } else {
        const err = await res.json();
        showToast(err.detail || "Erreur lors de l'envoi.", "error");
      }
    } catch (e) {
      showToast("Erreur réseau: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleRetryVideo = async (videoId) => {
    try {
      await fetch(`${API_BASE}/videos/${videoId}/retry`, { method: 'POST' });
      if (activeChannel) fetchChannelVideos(activeChannel.id);
      fetchAllVideos();
    } catch (e) {
      console.error(e);
    }
  };

  const handleDeleteChannel = async (channelId, e) => {
    e.stopPropagation();
    setOpenChannelMenuId(null);
    if (!confirm("Voulez-vous vraiment supprimer cette chaîne ? All videos and settings will be removed.")) return;
    try {
      await fetch(`${API_BASE}/channels/${channelId}`, { method: 'DELETE' });
      fetchChannels();
      if (activeChannel && activeChannel.id === channelId) {
        setActiveChannel(null);
        setView('channels');
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

  // Sample sentence for karaoke animation preview
  const sampleWords = [
    { text: "Le", highlight: previewWordIndex === 0 },
    { text: "calme", highlight: previewWordIndex === 1 },
    { text: "intérieur", highlight: previewWordIndex === 2 },
    { text: "dépend", highlight: previewWordIndex === 3 },
    { text: "de votre", highlight: previewWordIndex === 4 },
    { text: "esprit", highlight: previewWordIndex === 5 },
  ];

  return (
    <div className="font-body-md antialiased overflow-hidden flex h-screen bg-[#0f1217] text-[#e5e8f0]">
      
      {/* SIDE NAVBAR */}
      <nav className="hidden md:flex flex-col bg-[#141923] text-primary font-label-bold text-label-bold fixed left-0 top-0 h-screen w-[240px] z-40 border-r border-[#263042] py-6 justify-between">
        
        <div>
          {/* Brand Logo Header */}
          <div className="px-6 mb-8 flex items-center gap-3 cursor-pointer" onClick={() => setView('home')}>
            <div className="w-9 h-9 rounded-xl bg-gradient-to-tr from-[#00c2ff] to-[#0088ff] flex items-center justify-center text-slate-950 font-black text-lg shadow-lg shadow-[#00c2ff]/20">
              N
            </div>
            <div>
              <div className="font-title-sm text-base font-black text-white tracking-wide">NicheCut</div>
              <div className="text-slate-400 text-xs font-normal">Video Automation</div>
            </div>
          </div>

          {/* Navigation Links - Single Active Item Highlighted */}
          <div className="px-3 space-y-1.5">
            <button 
              onClick={() => setView('home')}
              className={`w-full flex items-center gap-3.5 px-4 py-3 cursor-pointer rounded-xl transition-all font-medium text-sm ${
                (view === 'home' || view === 'dashboard') 
                  ? 'bg-gradient-to-r from-[#00c2ff] to-[#0099ff] text-slate-950 font-bold shadow-md shadow-[#00c2ff]/20' 
                  : 'text-slate-300 hover:bg-[#1f2838] hover:text-white'
              }`}
            >
              <span className="material-symbols-outlined text-[20px]" style={{ fontVariationSettings: (view === 'home' || view === 'dashboard') ? "'FILL' 1" : "'FILL' 0" }}>home</span>
              Home
            </button>

            <button
              onClick={() => setView('channels')}
              className={`w-full flex items-center gap-3.5 px-4 py-3 cursor-pointer rounded-xl transition-all font-medium text-sm ${
                (view === 'channels' || view === 'channel_detail') 
                  ? 'bg-gradient-to-r from-[#00c2ff] to-[#0099ff] text-slate-950 font-bold shadow-md shadow-[#00c2ff]/20' 
                  : 'text-slate-300 hover:bg-[#1f2838] hover:text-white'
              }`}
            >
              <span className="material-symbols-outlined text-[20px]" style={{ fontVariationSettings: (view === 'channels' || view === 'channel_detail') ? "'FILL' 1" : "'FILL' 0" }}>subscriptions</span>
              Mes Chaînes
            </button>

            <button
              onClick={() => setView('videos')}
              className={`w-full flex items-center gap-3.5 px-4 py-3 cursor-pointer rounded-xl transition-all font-medium text-sm ${
                view === 'videos' 
                  ? 'bg-gradient-to-r from-[#00c2ff] to-[#0099ff] text-slate-950 font-bold shadow-md shadow-[#00c2ff]/20' 
                  : 'text-slate-300 hover:bg-[#1f2838] hover:text-white'
              }`}
            >
              <span className="material-symbols-outlined text-[20px]" style={{ fontVariationSettings: view === 'videos' ? "'FILL' 1" : "'FILL' 0" }}>movie</span>
              Mes Vidéos
            </button>

            {/* Main Action: Create Video Button */}
            <div className="pt-4 px-1">
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
                className="w-full flex items-center justify-center gap-2 px-4 py-3 cursor-pointer rounded-xl transition-all bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-sm shadow-lg shadow-emerald-500/25"
              >
                <span className="material-symbols-outlined text-[20px]" style={{ fontVariationSettings: "'FILL' 1" }}>add_circle</span>
                Nouvelle Vidéo
              </button>
            </div>
          </div>
        </div>

        {/* Bottom Sidebar User Profile Card (Paramètres & Profil integrated directly here) */}
        <div className="px-3">
          {currentUser ? (
            <div 
              onClick={() => setShowProfileModal(true)}
              className="p-3 bg-[#1b2230] hover:bg-[#252f42] rounded-xl border border-[#2b374d] cursor-pointer flex items-center gap-3 transition-all group shadow-sm"
              title="Cliquez pour ouvrir les Paramètres & Profil"
            >
              <div className="w-9 h-9 rounded-xl bg-[#00c2ff] text-slate-950 flex items-center justify-center font-bold text-sm flex-shrink-0 shadow-md">
                {currentUser.name.slice(0, 1).toUpperCase()}
              </div>
              <div className="truncate flex-1">
                <div className="text-xs text-white font-bold truncate group-hover:text-[#00c2ff] transition-colors">{currentUser.name}</div>
                <div className="text-[10px] text-slate-400 truncate">{currentUser.email}</div>
              </div>
              <span className="material-symbols-outlined text-[18px] text-slate-400 group-hover:text-white transition-colors">settings</span>
            </div>
          ) : (
            <button 
              onClick={() => setShowAuthModal(true)}
              className="w-full py-3 bg-[#00c2ff] text-slate-950 rounded-xl font-bold text-xs hover:bg-[#38d0ff] transition-colors flex items-center justify-center gap-2 shadow-md"
            >
              <span className="material-symbols-outlined text-[18px]">account_circle</span>
              Connexion / Inscription
            </button>
          )}
        </div>
      </nav>

      {/* MAIN CONTENT AREA */}
      <main className="flex-1 flex flex-col md:ml-[240px] h-screen overflow-hidden bg-[#0f1217]">
        
        {/* Top Header Bar */}
        <div className="hidden md:flex justify-between items-center px-8 py-5 border-b border-[#202938] bg-[#141923]/60 backdrop-blur-md">
          <h1 className="text-xl font-extrabold text-white tracking-tight flex items-center gap-3">
            {view === 'home' && 'Tableau de Bord'}
            {view === 'channels' && 'Vos Pipelines de Chaînes'}
            {view === 'videos' && 'Bibliothèque de Vidéos'}
            {view === 'wizard' && (wizardMode === 'edit' ? 'Modifier le Pipeline' : 'Assistant de Création de Chaîne')}
            {view === 'channel_detail' && (activeChannel ? `Chaîne: ${activeChannel.name}` : 'Détail Chaîne')}
          </h1>

          <div className="flex items-center gap-4">
            {/* Search Input */}
            <div className="relative focus-glow rounded-xl">
              <span className="material-symbols-outlined absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" style={{ fontSize: '18px' }}>search</span>
              <input 
                value={searchQuery}
                onChange={e => setSearchQuery(e.target.value)}
                className="bg-[#1b2230] border border-[#2b374d] rounded-xl pl-9 pr-4 py-2 text-xs text-white placeholder-slate-400 focus:outline-none w-60 transition-all" 
                placeholder="Rechercher une chaîne..." 
                type="text"
              />
            </div>

          </div>
        </div>

        {/* Scrollable Canvas View Content */}
        <div className="flex-1 overflow-y-auto p-6 md:p-8">
          <div className="max-w-[1400px] mx-auto space-y-8">
            
            {/* VIEW 1: HOME / DASHBOARD OVERVIEW */}
            {(view === 'home' || view === 'dashboard') && (
              <>
                {/* Stats Row Bento Cards */}
                <section className="grid grid-cols-1 md:grid-cols-3 gap-5">
                  <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-6 flex flex-col justify-between shadow-lg relative overflow-hidden group">
                    <div className="absolute -right-4 -bottom-4 w-24 h-24 bg-[#00c2ff]/5 rounded-full blur-xl group-hover:bg-[#00c2ff]/10 transition-all"></div>
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider">Chaînes Actives</h3>
                      <div className="p-2 rounded-xl bg-[#00c2ff]/10 text-[#00c2ff]">
                        <span className="material-symbols-outlined text-[22px]" style={{ fontVariationSettings: "'FILL' 1" }}>cell_tower</span>
                      </div>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="text-4xl font-extrabold text-white">{channels.length}</span>
                      <span className="text-xs font-bold text-[#00c2ff] bg-[#00c2ff]/10 px-2.5 py-1 rounded-lg">Configurées</span>
                    </div>
                    <div className="w-full h-1.5 bg-[#202938] mt-4 rounded-full overflow-hidden">
                      <div className="h-full bg-gradient-to-r from-[#00c2ff] to-[#0088ff] w-full rounded-full"></div>
                    </div>
                  </div>

                  <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-6 flex flex-col justify-between shadow-lg relative overflow-hidden group">
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider">Vidéos en Attente</h3>
                      <div className="p-2 rounded-xl bg-amber-500/10 text-amber-400">
                        <span className="material-symbols-outlined text-[22px]" style={{ fontVariationSettings: "'FILL' 1" }}>hourglass_empty</span>
                      </div>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="text-4xl font-extrabold text-white">{totalQueued}</span>
                      <span className="text-xs font-medium text-amber-400 bg-amber-500/10 px-2.5 py-1 rounded-lg">En cours de rendu...</span>
                    </div>
                    <div className="w-full h-1.5 bg-[#202938] mt-4 rounded-full overflow-hidden flex">
                      <div className="h-full bg-amber-400 w-2/3 animate-pulse"></div>
                    </div>
                  </div>

                  <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-6 flex flex-col justify-between shadow-lg relative overflow-hidden group">
                    <div className="flex justify-between items-start mb-4">
                      <h3 className="text-xs font-bold text-slate-400 uppercase tracking-wider">Vidéos Terminées</h3>
                      <div className="p-2 rounded-xl bg-emerald-500/10 text-emerald-400">
                        <span className="material-symbols-outlined text-[22px]" style={{ fontVariationSettings: "'FILL' 1" }}>task_alt</span>
                      </div>
                    </div>
                    <div className="flex items-end justify-between">
                      <span className="text-4xl font-extrabold text-white">{totalCompleted}</span>
                      <span className="text-xs font-bold text-emerald-400 bg-emerald-500/10 px-2.5 py-1 rounded-lg">Prêtes à publier</span>
                    </div>
                    <div className="w-full h-1.5 bg-[#202938] mt-4 rounded-full overflow-hidden">
                      <div className="h-full bg-emerald-400 w-full rounded-full"></div>
                    </div>
                  </div>
                </section>

                {/* Quick Launch Banner */}
                <section className="bg-gradient-to-r from-[#161b22] via-[#1a2332] to-[#161b22] border border-[#263042] rounded-2xl p-6 flex flex-col md:flex-row items-center justify-between gap-6 shadow-xl">
                  <div className="space-y-1">
                    <h3 className="text-lg font-bold text-white flex items-center gap-2">
                      <span className="material-symbols-outlined text-[#00c2ff]">auto_awesome</span>
                      Générateur de Vidéo Automatisé
                    </h3>
                    <p className="text-sm text-slate-400">
                      Générez une vidéo YouTube longue durée (16:9) complète avec sous-titres karaoké, voix off IA et montage visuel en 1 clic.
                    </p>
                  </div>
                  <button
                    onClick={() => {
                      if (channels.length > 0) {
                        setActiveChannel(channels[0]);
                        setShowSubmitModal(true);
                      } else {
                        openCreateWizard();
                      }
                    }}
                    className="px-6 py-3 bg-[#00c2ff] hover:bg-[#38d0ff] text-slate-950 font-bold text-sm rounded-xl transition-all flex items-center gap-2 shadow-lg shadow-[#00c2ff]/20 flex-shrink-0"
                  >
                    <span className="material-symbols-outlined text-[20px]">videocam</span>
                    Lancer une Génération
                  </button>
                </section>

                {/* Pipelines Preview in Home */}
                <section>
                  <div className="flex justify-between items-center mb-5">
                    <h3 className="text-lg font-bold text-white">Aperçu des Chaînes</h3>
                    <button onClick={() => setView('channels')} className="text-xs font-bold text-[#00c2ff] hover:underline flex items-center gap-1">
                      Voir toutes les chaînes ({channels.length}) <span className="material-symbols-outlined text-[16px]">arrow_forward</span>
                    </button>
                  </div>

                  {channels.length === 0 ? (
                    <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-10 text-center">
                      <span className="material-symbols-outlined text-[48px] text-slate-500 mb-3">subscriptions</span>
                      <h4 className="text-base font-bold text-white mb-1">Aucune chaîne configurée</h4>
                      <p className="text-xs text-slate-400 mb-5">Créez votre première chaîne pour automatiser le montage.</p>
                      <button onClick={openCreateWizard} className="px-5 py-2.5 bg-[#00c2ff] text-slate-950 font-bold text-xs rounded-xl">
                        + Créer une chaîne
                      </button>
                    </div>
                  ) : (
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
                      {channels.slice(0, 3).map(chan => {
                        const statusInfo = getChannelStatusInfo(chan);
                        return (
                          <div 
                            key={chan.id} 
                            onClick={() => { setActiveChannel(chan); fetchChannelVideos(chan.id); setView('channel_detail'); }}
                            className="bg-[#161b22] border border-[#263042] hover:border-[#00c2ff]/40 rounded-2xl p-5 cursor-pointer transition-all hover:-translate-y-1 shadow-md space-y-4"
                          >
                            <div className="flex items-center gap-3">
                              {getChannelLogoUrl(chan) ? (
                                <img src={getChannelLogoUrl(chan)} alt={chan.name} className="w-12 h-12 rounded-xl object-cover border border-[#2b374d]" />
                              ) : (
                                <div className="w-12 h-12 rounded-xl bg-[#1b2230] text-[#00c2ff] font-extrabold flex items-center justify-center text-lg border border-[#2b374d]">
                                  {chan.name.slice(0, 2).toUpperCase()}
                                </div>
                              )}
                              <div className="min-w-0 flex-1">
                                <h4 className="font-bold text-white text-sm truncate">{chan.name}</h4>
                                <span className="text-xs text-slate-400 block truncate">{chan.niche}</span>
                              </div>
                            </div>
                            <div className="flex items-center justify-between text-xs pt-2 border-t border-[#202938]">
                              <span className={`px-2.5 py-1 rounded-lg font-bold text-[11px] uppercase ${statusInfo.className}`}>
                                {statusInfo.label}
                              </span>
                              <span className="text-slate-400 font-mono">{chan.done_count || 0} vidéos prêtes</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>
              </>
            )}

            {/* VIEW 2: MES CHAÎNES (Dedicated Channel Pipelines Cards List with 3-Dots Menu) */}
            {view === 'channels' && (
              <section className="space-y-6">
                <div>
                  <h2 className="text-xl font-extrabold text-white">Vos Pipelines de Chaînes</h2>
                  <p className="text-xs text-slate-400 mt-1">Configurez l'identité, les sous-titres et les effets de vos chaînes automatiques.</p>
                </div>

                {filteredChannels.length === 0 ? (
                  <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-12 text-center">
                    <span className="material-symbols-outlined text-[54px] text-slate-500 mb-4">video_settings</span>
                    <h3 className="text-lg font-bold text-white mb-2">Aucune chaîne trouvée</h3>
                    <p className="text-sm text-slate-400 mb-6 max-w-md mx-auto">
                      Configurez votre premier pipeline vidéo (sous-titres karaoké, logo, musique de fond, images) et générez sans limite.
                    </p>
                    <button 
                      onClick={openCreateWizard}
                      className="bg-[#00c2ff] text-slate-950 px-6 py-3 rounded-xl font-bold text-sm hover:bg-[#38d0ff] transition-all shadow-lg inline-flex items-center gap-2"
                    >
                      <span className="material-symbols-outlined">add</span> Créer un Pipeline de Chaîne
                    </button>
                  </div>
                ) : (
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-6">
                    {filteredChannels.map(chan => {
                      const logoUrl = getChannelLogoUrl(chan);
                      const statusInfo = getChannelStatusInfo(chan);
                      const isMenuOpen = openChannelMenuId === chan.id;

                      return (
                        <div
                          key={chan.id}
                          onClick={() => { setActiveChannel(chan); fetchChannelVideos(chan.id); setView('channel_detail'); }}
                          className="bg-[#161b22] hover:bg-[#1c232e] border border-[#263042] hover:border-[#00c2ff]/40 rounded-2xl p-5 transition-all cursor-pointer group flex flex-col justify-between min-h-[220px] shadow-lg relative card-warm-hover channel-menu-container"
                        >
                          {/* Card Header & 3-Dots Action Button */}
                          <div className="flex items-start justify-between gap-3">
                            <div className="flex items-center gap-3.5 min-w-0">
                              {logoUrl ? (
                                <img src={logoUrl} alt={chan.name} className="w-12 h-12 rounded-xl object-cover border border-[#2b374d] flex-shrink-0 shadow-md" />
                              ) : (
                                <div className="w-12 h-12 rounded-xl bg-gradient-to-tr from-[#004c66] to-[#007f99] text-[#c2e8ff] flex items-center justify-center font-black text-lg flex-shrink-0 border border-[#00c2ff]/30 shadow-md">
                                  {chan.name.slice(0, 2).toUpperCase()}
                                </div>
                              )}
                              <div className="min-w-0">
                                <h4 className="font-bold text-base text-white group-hover:text-[#00c2ff] transition-colors truncate">{chan.name}</h4>
                                <span className="text-xs font-medium text-slate-400 truncate block mt-0.5">{chan.niche}</span>
                              </div>
                            </div>

                            {/* 3-Dots Menu Button (Kebab Menu) */}
                            <div className="relative flex-shrink-0">
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  setOpenChannelMenuId(isMenuOpen ? null : chan.id);
                                }}
                                className="p-2 rounded-xl hover:bg-[#2a3547] text-slate-400 hover:text-white transition-colors"
                                title="Actions chaîne"
                              >
                                <span className="material-symbols-outlined text-[20px]">more_vert</span>
                              </button>

                              {/* Dropdown Popup Menu */}
                              {isMenuOpen && (
                                <div className="absolute right-0 top-10 w-48 bg-[#1f2838] border border-[#2d3a52] rounded-xl shadow-2xl z-50 py-1.5 animate-in fade-in duration-150">
                                  <button
                                    onClick={(e) => openEditWizard(chan, e)}
                                    className="w-full text-left px-4 py-2.5 text-xs text-slate-200 hover:bg-[#2c394e] hover:text-white flex items-center gap-2 font-medium"
                                  >
                                    <span className="material-symbols-outlined text-[16px] text-[#00c2ff]">edit</span>
                                    Modifier la chaîne
                                  </button>
                                  <div className="h-[1px] bg-[#2d3a52] my-1"></div>
                                  <button
                                    onClick={(e) => handleDeleteChannel(chan.id, e)}
                                    className="w-full text-left px-4 py-2.5 text-xs text-rose-400 hover:bg-rose-950/50 flex items-center gap-2 font-medium"
                                  >
                                    <span className="material-symbols-outlined text-[16px]">delete</span>
                                    Supprimer la chaîne
                                  </button>
                                </div>
                              )}
                            </div>
                          </div>

                          {/* Status Tag */}
                          <div className="mt-4">
                            <span className={`inline-block px-3 py-1 rounded-lg text-[11px] font-mono font-bold uppercase tracking-wider ${statusInfo.className}`}>
                              {statusInfo.label}
                            </span>
                          </div>

                          {/* Counters Grid */}
                          <div className="grid grid-cols-2 gap-2 mt-4">
                            <div className="bg-[#11151c] p-2.5 rounded-xl border border-[#202938]">
                              <div className="text-[10px] font-bold uppercase text-slate-400 tracking-wider">En File</div>
                              <div className="text-base text-[#00c2ff] font-extrabold mt-0.5">{(chan.queued_count || 0) + (chan.rendering_count || 0)}</div>
                            </div>
                            <div className="bg-[#11151c] p-2.5 rounded-xl border border-[#202938]">
                              <div className="text-[10px] font-bold uppercase text-slate-400 tracking-wider">Vidéos Prêtes</div>
                              <div className="text-base text-white font-extrabold mt-0.5">{chan.done_count || 0}</div>
                            </div>
                          </div>

                        </div>
                      );
                    })}

                    {/* Add Channel Card — single entry point to create a channel */}
                    <button
                      onClick={openCreateWizard}
                      className="rounded-2xl p-5 border-2 border-dashed border-[#2b374d] hover:border-[#00c2ff] hover:bg-[#161b22] transition-all flex flex-col items-center justify-center gap-3 min-h-[220px] text-slate-400 hover:text-[#00c2ff] group"
                    >
                      <div className="w-14 h-14 rounded-full bg-[#1b2230] group-hover:bg-[#00c2ff]/10 flex items-center justify-center transition-colors">
                        <span className="material-symbols-outlined text-[28px]">add</span>
                      </div>
                      <span className="font-bold text-sm">Ajouter une Chaîne</span>
                    </button>
                  </div>
                )}
              </section>
            )}

            {/* VIEW 3: MES VIDÉOS (Videos Library View) */}
            {view === 'videos' && (
              <section className="space-y-6">
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                  <div>
                    <h2 className="text-xl font-extrabold text-white">Bibliothèque de Vidéos</h2>
                    <p className="text-xs text-slate-400 mt-1">Historique de tous les sujets de vidéos rendus ou en cours de traitement.</p>
                  </div>
                  
                  {/* Channel Filter Selector */}
                  <div className="flex items-center gap-3">
                    <label className="text-xs text-slate-400 font-bold">Filtrer par chaîne:</label>
                    <select
                      value={videoFilterChannelId}
                      onChange={e => setVideoFilterChannelId(e.target.value)}
                      className="bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-2 text-xs text-white focus:outline-none"
                    >
                      <option value="all">Toutes les chaînes ({channels.length})</option>
                      {channels.map(c => (
                        <option key={c.id} value={c.id}>{c.name}</option>
                      ))}
                    </select>
                  </div>
                </div>

                {/* Videos List Container */}
                {allVideos.length === 0 ? (
                  <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-12 text-center">
                    <span className="material-symbols-outlined text-[54px] text-slate-500 mb-3">movie</span>
                    <h3 className="text-base font-bold text-white mb-1">Aucune vidéo dans l'historique</h3>
                    <p className="text-xs text-slate-400 mb-5">Lancez une nouvelle génération depuis la barre latérale ou la liste des chaînes.</p>
                    <button
                      onClick={() => {
                        if (channels.length > 0) {
                          setActiveChannel(channels[0]);
                          setShowSubmitModal(true);
                        } else {
                          openCreateWizard();
                        }
                      }}
                      className="px-5 py-2.5 bg-[#00c2ff] text-slate-950 font-bold text-xs rounded-xl"
                    >
                      + Nouvelle Vidéo
                    </button>
                  </div>
                ) : (
                  <div className="space-y-3">
                    {allVideos
                      .filter(v => videoFilterChannelId === 'all' || v.channel_id === videoFilterChannelId)
                      .map(vid => {
                        const channelObj = channels.find(c => c.id === vid.channel_id);
                        return (
                          <div key={vid.id} className="bg-[#161b22] rounded-2xl p-5 flex flex-col md:flex-row md:items-center justify-between gap-4 border border-[#263042] hover:border-[#00c2ff]/30 transition-all">
                            <div className="space-y-2 max-w-[70%]">
                              <div className="flex items-center gap-3 flex-wrap">
                                <span className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold uppercase ${
                                  vid.status === 'done' ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' :
                                  vid.status === 'rendering' ? 'bg-blue-950 text-blue-300 border border-blue-800 animate-pulse' :
                                  vid.status === 'failed' ? 'bg-rose-950 text-rose-300 border border-rose-800' :
                                  'bg-amber-950 text-amber-300 border border-amber-800'
                                }`}>
                                  {vid.status}
                                </span>
                                {channelObj && (
                                  <span className="text-xs font-bold text-[#00c2ff] bg-[#00c2ff]/10 px-2.5 py-0.5 rounded-lg border border-[#00c2ff]/20">
                                    {channelObj.name}
                                  </span>
                                )}
                                <span className="text-xs text-slate-400 font-mono">
                                  Mode: {vid.input_type === 'audio' ? 'Audio importé' : 'Texte Izivoice'}
                                </span>
                              </div>
                              <p className="text-white text-sm line-clamp-2 italic font-medium">
                                "{vid.script_text}"
                              </p>
                            </div>

                            <div className="flex items-center gap-3 flex-shrink-0">
                              {vid.status === 'done' && (
                                <button 
                                  onClick={() => setSelectedVideo(vid)}
                                  className="px-4 py-2 bg-[#00c2ff] text-slate-950 rounded-xl font-bold text-xs hover:bg-[#38d0ff] transition-all flex items-center gap-2 shadow-md shadow-[#00c2ff]/20"
                                >
                                  <span className="material-symbols-outlined text-[18px]">play_circle</span> Voir Vidéo
                                </button>
                              )}
                              {vid.status === 'failed' && (
                                <button 
                                  onClick={() => handleRetryVideo(vid.id)}
                                  className="px-4 py-2 bg-[#1f2838] text-white rounded-xl font-bold text-xs hover:bg-[#2b384e] transition-all flex items-center gap-2 border border-[#2b374d]"
                                >
                                  <span className="material-symbols-outlined text-[18px]">refresh</span> Relancer
                                </button>
                              )}
                            </div>
                          </div>
                        );
                      })}
                  </div>
                )}
              </section>
            )}

            {/* VIEW 4: CHANNEL DETAIL VIEW */}
            {view === 'channel_detail' && activeChannel && (
              <div className="space-y-8">
                <section className="bg-[#161b22] border border-[#263042] rounded-2xl p-6 flex flex-col md:flex-row md:items-center justify-between gap-6 shadow-xl">
                  <div className="flex items-center gap-5 min-w-0">
                    {getChannelLogoUrl(activeChannel) ? (
                      <img src={getChannelLogoUrl(activeChannel)} alt={activeChannel.name} className="w-20 h-20 rounded-2xl object-cover border border-[#2b374d] shadow-lg flex-shrink-0" />
                    ) : (
                      <div className="w-20 h-20 rounded-2xl bg-gradient-to-tr from-[#004c66] to-[#007f99] text-[#c2e8ff] font-black text-2xl flex items-center justify-center border border-[#00c2ff]/40 flex-shrink-0 shadow-lg">
                        {activeChannel.name.slice(0, 2).toUpperCase()}
                      </div>
                    )}
                    <div className="min-w-0">
                      <h1 className="text-2xl font-extrabold text-white truncate">{activeChannel.name}</h1>
                      <div className="flex items-center gap-3 text-slate-400 text-xs font-medium mt-1">
                        <span>Niche: <strong className="text-white">{activeChannel.niche}</strong></span>
                        <span>•</span>
                        <span className="font-mono">ID: {activeChannel.id.slice(0, 8)}</span>
                      </div>
                      {(() => {
                        const s = getChannelStatusInfo(activeChannel);
                        return <span className={`inline-block mt-2.5 px-3 py-1 rounded-lg text-[11px] font-mono font-bold uppercase tracking-wider ${s.className}`}>{s.label}</span>;
                      })()}
                    </div>
                  </div>

                  <div className="flex items-center gap-3 flex-shrink-0">
                    <button
                      onClick={(e) => openEditWizard(activeChannel, e)}
                      className="px-4 py-2.5 bg-[#1b2230] text-white rounded-xl font-bold text-xs hover:bg-[#252f42] transition-colors flex items-center gap-2 border border-[#2b374d]"
                    >
                      <span className="material-symbols-outlined text-[18px]">edit</span>
                      Modifier le Pipeline
                    </button>
                    <button
                      onClick={() => setShowSubmitModal(true)}
                      className="px-5 py-2.5 bg-[#00c2ff] text-slate-950 rounded-xl font-bold text-xs hover:bg-[#38d0ff] transition-all flex items-center gap-2 shadow-lg shadow-[#00c2ff]/20"
                    >
                      <span className="material-symbols-outlined text-[18px]">add</span>
                      Nouvelle Vidéo
                    </button>
                  </div>
                </section>

                <section className="space-y-4">
                  <h3 className="text-lg font-bold text-white">Vidéos de la Chaîne ({channelVideos.length})</h3>
                  {channelVideos.length === 0 ? (
                    <div className="bg-[#161b22] border border-[#263042] rounded-2xl p-10 text-center">
                      <span className="material-symbols-outlined text-[40px] text-slate-500 mb-2">description</span>
                      <h4 className="text-base font-bold text-white mb-1">Aucune vidéo soumise</h4>
                      <p className="text-xs text-slate-400 mb-5">Soumettez votre premier sujet (texte de script ou fichiers audio).</p>
                      <button 
                        onClick={() => setShowSubmitModal(true)}
                        className="bg-[#00c2ff] text-slate-950 px-5 py-2.5 rounded-xl font-bold text-xs hover:bg-[#38d0ff] transition-all"
                      >
                        Soumettre un sujet de vidéo
                      </button>
                    </div>
                  ) : (
                    <div className="space-y-3">
                      {channelVideos.map(vid => (
                        <div key={vid.id} className="bg-[#161b22] rounded-2xl p-5 flex justify-between items-center border border-[#263042]">
                          <div className="space-y-1.5 max-w-[70%]">
                            <div className="flex items-center gap-3">
                              <span className={`px-2.5 py-1 rounded-md text-[11px] font-mono font-bold uppercase ${
                                vid.status === 'done' ? 'bg-emerald-950 text-emerald-300 border border-emerald-800' :
                                vid.status === 'rendering' ? 'bg-blue-950 text-blue-300 border border-blue-800 animate-pulse' :
                                vid.status === 'failed' ? 'bg-rose-950 text-rose-300 border border-rose-800' :
                                'bg-amber-950 text-amber-300 border border-amber-800'
                              }`}>
                                {vid.status}
                              </span>
                              <span className="text-xs text-slate-400 font-mono">
                                Mode: {vid.input_type === 'audio' ? 'Audio importé' : 'Texte Izivoice'}
                              </span>
                            </div>
                            <p className="text-white text-sm line-clamp-1 italic font-medium">
                              "{vid.script_text}"
                            </p>
                          </div>

                          <div className="flex gap-2">
                            {vid.status === 'done' && (
                              <button 
                                onClick={() => setSelectedVideo(vid)}
                                className="px-4 py-2 bg-[#00c2ff] text-slate-950 rounded-xl font-bold text-xs hover:bg-[#38d0ff] transition-all flex items-center gap-1.5"
                              >
                                <span className="material-symbols-outlined text-[16px]">play_circle</span> Voir Vidéo
                              </button>
                            )}
                            {vid.status === 'failed' && (
                              <button 
                                onClick={() => handleRetryVideo(vid.id)}
                                className="px-4 py-2 bg-[#1b2230] text-white rounded-xl font-bold text-xs hover:bg-[#252f42] transition-all flex items-center gap-1.5 border border-[#2b374d]"
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

            {/* VIEW 5: CHANNEL WIZARD (CREATE / EDIT) */}
            {view === 'wizard' && (
              <div className="max-w-[900px] mx-auto bg-[#161b22] border border-[#263042] rounded-3xl p-8 shadow-2xl space-y-8">
                {/* Wizard Header Stepper */}
                <div className="flex items-center justify-between border-b border-[#263042] pb-6">
                  <div>
                    <h2 className="text-xl font-extrabold text-white">
                      {wizardMode === 'edit' ? 'Modifier le Pipeline de la Chaîne' : 'Assistant de Configuration de Chaîne'}
                    </h2>
                    <p className="text-xs text-slate-400 mt-1">Étape {wizardStep} sur 5</p>
                  </div>
                  <button onClick={() => setView('channels')} className="text-slate-400 hover:text-white p-2">
                    <span className="material-symbols-outlined">close</span>
                  </button>
                </div>

                {/* Steps Timeline Indicator */}
                <div className="grid grid-cols-5 gap-2">
                  {['Identité', 'Sous-titres', 'Musique', 'Visuels', 'Aperçu Final'].map((label, idx) => {
                    const stepNum = idx + 1;
                    const isActive = wizardStep === stepNum;
                    const isPassed = wizardStep > stepNum;
                    return (
                      <button
                        key={stepNum}
                        onClick={() => setWizardStep(stepNum)}
                        className={`py-2 px-1 text-center rounded-xl text-xs font-bold transition-all ${
                          isActive ? 'bg-[#00c2ff] text-slate-950 shadow-md' :
                          isPassed ? 'bg-[#00c2ff]/20 text-[#00c2ff] border border-[#00c2ff]/40' :
                          'bg-[#1b2230] text-slate-400'
                        }`}
                      >
                        {stepNum}. {label}
                      </button>
                    );
                  })}
                </div>

                {/* STEP 1: INFORMATIONS GÉNÉRALES & IDENTITÉ (CONTIENT LOGO, NOM & FILIGRANE) */}
                {wizardStep === 1 && (
                  <div className="space-y-6">
                    <h3 className="text-base font-bold text-white">1. Identité de la Chaîne & Filigrane</h3>
                    
                    <div>
                      <label className="block text-xs font-bold text-slate-300 mb-2">Photo / Logo de la chaîne</label>
                      <div className="flex items-center gap-5">
                        <div
                          onClick={() => logoInputRef.current && logoInputRef.current.click()}
                          className="w-24 h-24 rounded-2xl bg-[#1b2230] border-2 border-dashed border-[#2b374d] hover:border-[#00c2ff] cursor-pointer flex items-center justify-center overflow-hidden flex-shrink-0 transition-colors group"
                        >
                          {logoPreviewUrl ? (
                            <img src={logoPreviewUrl} alt="Logo" className="w-full h-full object-cover" />
                          ) : (
                            <span className="material-symbols-outlined text-slate-400 group-hover:text-[#00c2ff] text-[32px]">add_a_photo</span>
                          )}
                        </div>
                        <div>
                          <input
                            type="file"
                            ref={logoInputRef}
                            accept="image/*"
                            onChange={handleLogoFileSelect}
                            className="hidden"
                          />
                          <button
                            type="button"
                            onClick={() => logoInputRef.current && logoInputRef.current.click()}
                            className="px-4 py-2.5 bg-[#1b2230] text-white rounded-xl font-bold text-xs hover:bg-[#252f42] transition-colors border border-[#2b374d]"
                          >
                            {logoPreviewUrl ? "Changer l'image" : "Sélectionner un logo"}
                          </button>
                          <p className="text-[11px] text-slate-400 mt-2">Format conseillé: PNG ou JPG carré (512x512)</p>
                        </div>
                      </div>
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                      <div>
                        <label className="block text-xs font-bold text-slate-300 mb-2">Nom de la chaîne YouTube / TikTok</label>
                        <input
                          value={newChannel.name}
                          onChange={e => setNewChannel({ ...newChannel, name: e.target.value })}
                          className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-3 text-sm text-white focus:border-[#00c2ff] outline-none"
                          placeholder="Ex: Stoic Mind Daily"
                        />
                      </div>

                      <div>
                        <label className="block text-xs font-bold text-slate-300 mb-2">Niche de contenu</label>
                        <select 
                          value={newChannel.niche}
                          onChange={e => setNewChannel({ ...newChannel, niche: e.target.value })}
                          className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-3 text-sm text-white focus:border-[#00c2ff] outline-none"
                        >
                          <option value="Philosophie & Stoïcisme">Philosophie & Stoïcisme</option>
                          <option value="Spiritualité & Méditation">Spiritualité & Méditation</option>
                          <option value="Religion & Récits Antiquité">Religion & Récits Antiquité</option>
                          <option value="Développement Personnel">Développement Personnel</option>
                          <option value="Histoires & Récits Captivants">Histoires & Récits Captivants</option>
                        </select>
                      </div>
                    </div>

                    {/* Branding Filigrane Integrated in Step 1 */}
                    <div className="pt-4 border-t border-[#263042] space-y-4">
                      <label className="block text-xs font-bold text-[#00c2ff]">Filigrane de Marque / Watermark Overlay</label>
                      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                        <div>
                          <label className="block text-[11px] font-bold text-slate-300 mb-1">Texte Filigrane (@Handle)</label>
                          <input 
                            value={newChannel.branding.channel_name_text}
                            onChange={e => setNewChannel({ ...newChannel, branding: { ...newChannel.branding, channel_name_text: e.target.value } })}
                            className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-3 py-2 text-xs text-white focus:border-[#00c2ff] outline-none"
                            placeholder="ex: @StoicMindDaily"
                          />
                        </div>
                        <div>
                          <label className="block text-[11px] font-bold text-slate-300 mb-1">Position</label>
                          <select
                            value={newChannel.branding.watermark_position || 'top_right'}
                            onChange={e => setNewChannel({ ...newChannel, branding: { ...newChannel.branding, watermark_position: e.target.value } })}
                            className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-3 py-2 text-xs text-white focus:border-[#00c2ff] outline-none"
                          >
                            <option value="top_right">Haut Droite</option>
                            <option value="bottom_right">Bas Droite</option>
                            <option value="top_left">Haut Gauche</option>
                            <option value="bottom_left">Bas Gauche</option>
                          </select>
                        </div>
                        <div>
                          <label className="block text-[11px] font-bold text-slate-300 mb-1">Opacité ({Math.round((newChannel.branding.watermark_opacity || 0.85) * 100)}%)</label>
                          <input
                            type="range"
                            min="0.2"
                            max="1.0"
                            step="0.05"
                            value={newChannel.branding.watermark_opacity || 0.85}
                            onChange={e => setNewChannel({ ...newChannel, branding: { ...newChannel.branding, watermark_opacity: parseFloat(e.target.value) } })}
                            className="w-full accent-[#00c2ff]"
                          />
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* STEP 2: SOUS-TITRES & KARAOKÉ ASS */}
                {wizardStep === 2 && (
                  <div className="space-y-6">
                    <div className="flex justify-between items-center">
                      <h3 className="text-base font-bold text-white">2. Personnalisation Avancée des Sous-Titres Karaoké</h3>
                      <span className="text-xs font-mono text-[#00c2ff] bg-[#00c2ff]/10 px-2.5 py-1 rounded-lg">Fichier ASS Ultra-Fluid</span>
                    </div>

                    {/* Presets Grid */}
                    <div>
                      <label className="block text-xs font-bold text-slate-300 mb-2">Presets de style sous-titre recommandés</label>
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                        {SUBTITLE_PRESETS.map(preset => (
                          <button
                            key={preset.id}
                            type="button"
                            onClick={() => {
                              setNewChannel(prev => ({
                                ...prev,
                                subtitle_style: {
                                  ...prev.subtitle_style,
                                  font: preset.font,
                                  size: preset.size,
                                  color: preset.color,
                                  outline_color: preset.outline_color,
                                  outline_width: preset.outline_width,
                                  box_color: preset.box_color
                                }
                              }));
                            }}
                            className="p-3 bg-[#1b2230] hover:bg-[#252f42] border border-[#2b374d] rounded-xl text-left transition-all hover:border-[#00c2ff]"
                          >
                            <div className="text-xs font-bold text-white">{preset.name}</div>
                            <div className="text-[10px] text-slate-400 mt-1 font-mono">{preset.font} • {preset.size}px</div>
                          </button>
                        ))}
                      </div>
                    </div>

                    {/* Custom Controls */}
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                      <div>
                        <label className="block text-xs font-bold text-slate-300 mb-2">Police (Font)</label>
                        <select 
                          value={newChannel.subtitle_style.font}
                          onChange={e => setNewChannel({ ...newChannel, subtitle_style: { ...newChannel.subtitle_style, font: e.target.value } })}
                          className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-2.5 text-xs text-white focus:border-[#00c2ff] outline-none"
                        >
                          <option value="Montserrat">Montserrat (Modern Heavy)</option>
                          <option value="Inter">Inter (Clean Bold)</option>
                          <option value="Impact">Impact (TikTok Heavy)</option>
                          <option value="Bebas Neue">Bebas Neue (Tall Display)</option>
                          <option value="Oswald">Oswald (Condensed)</option>
                          <option value="Arial">Arial (Classic)</option>
                        </select>
                      </div>

                      <div>
                        <label className="block text-xs font-bold text-slate-300 mb-2">Taille du Texte ({newChannel.subtitle_style.size}px)</label>
                        <input 
                          type="range"
                          min="28"
                          max="64"
                          value={newChannel.subtitle_style.size}
                          onChange={e => setNewChannel({ ...newChannel, subtitle_style: { ...newChannel.subtitle_style, size: parseInt(e.target.value) || 44 } })}
                          className="w-full accent-[#00c2ff]"
                        />
                      </div>

                      <div>
                        <label className="block text-xs font-bold text-slate-300 mb-2">Couleur Principale</label>
                        <div className="flex items-center gap-2">
                          <input 
                            type="color"
                            value={newChannel.subtitle_style.color.startsWith('#') ? newChannel.subtitle_style.color : '#FFD700'}
                            onChange={e => setNewChannel({ ...newChannel, subtitle_style: { ...newChannel.subtitle_style, color: e.target.value } })}
                            className="w-10 h-10 rounded-xl bg-[#1b2230] border border-[#2b374d] cursor-pointer"
                          />
                          <span className="text-xs font-mono text-slate-300">{newChannel.subtitle_style.color}</span>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                {/* STEP 3: MUSIQUE DE FOND & AUDIO */}
                {wizardStep === 3 && (
                  <div className="space-y-6">
                    <h3 className="text-base font-bold text-white">3. Musique de Fond Ambiante & Auto-Ducking</h3>
                    
                    <div>
                      <label className="block text-xs font-bold text-slate-300 mb-2">Ambiance Musicale</label>
                      <select 
                        value={newChannel.music_preference.track_id_or_style}
                        onChange={e => setNewChannel({ ...newChannel, music_preference: { ...newChannel.music_preference, track_id_or_style: e.target.value } })}
                        className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-3 text-sm text-white focus:border-[#00c2ff] outline-none"
                      >
                        <option value="ambient">Zen & Méditation (Ambiant Relax)</option>
                        <option value="dramatic">Dark Ambient Stoïcien & Profond</option>
                        <option value="cinematic">Cinématique Épique & Émotionnel</option>
                        <option value="lofi">Lo-Fi Chill & Focus</option>
                      </select>
                    </div>

                    <div>
                      <label className="block text-xs font-bold text-slate-300 mb-2">Volume Musique ({Math.round((newChannel.music_preference.volume || 0.15) * 100)}%)</label>
                      <input 
                        type="range"
                        min="0.05"
                        max="0.5"
                        step="0.01"
                        value={newChannel.music_preference.volume || 0.15}
                        onChange={e => setNewChannel({ ...newChannel, music_preference: { ...newChannel.music_preference, volume: parseFloat(e.target.value) } })}
                        className="w-full accent-[#00c2ff]"
                      />
                    </div>
                  </div>
                )}

                {/* STEP 4: VISUELS & SOURCES D'IMAGES (OPTION A, OPTION B, OPTION C HYBRIDE) */}
                {wizardStep === 4 && (
                  <div className="space-y-6">
                    <div>
                      <h3 className="text-base font-bold text-white">4. Source d'Images Visuelles & Mode de Génération</h3>
                      <p className="text-xs text-slate-400 mt-1">Choisissez la provenance des visuels pour votre vidéo (Dossier local, IA intégrale, ou Mode Hybride).</p>
                    </div>

                    {/* 3 CARDS SELECTION GRID */}
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-5">
                      
                      {/* OPTION A: DOSSIER IMAGES LOCALES */}
                      <div 
                        onClick={() => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, source: 'library' } })}
                        className={`p-5 rounded-2xl border-2 transition-all cursor-pointer space-y-4 flex flex-col justify-between ${
                          newChannel.image_style.source === 'library'
                            ? 'bg-[#1b2230] border-[#00c2ff] shadow-lg shadow-[#00c2ff]/10'
                            : 'bg-[#141923] border-[#263042] hover:border-slate-500'
                        }`}
                      >
                        <div className="space-y-2">
                          <div className="flex items-center gap-2.5">
                            <span className="material-symbols-outlined text-[#00c2ff] text-[24px]">folder_open</span>
                            <h4 className="font-bold text-white text-xs">Option A: Importer un dossier local</h4>
                          </div>
                          <p className="text-[11px] text-slate-400">
                            Sélectionnez un dossier complet contenant toutes les images de votre machine.
                          </p>
                        </div>

                        {/* Folder Picker & File Fallback Dropzone */}
                        <div
                          onDragOver={(e) => { e.preventDefault(); setIsFolderDragging(true); }}
                          onDragLeave={() => setIsFolderDragging(false)}
                          onDrop={handleFolderDrop}
                          className={`border-2 border-dashed rounded-xl p-4 text-center cursor-pointer transition-all ${
                            isFolderDragging ? 'border-[#00c2ff] bg-[#00c2ff]/10' : 'border-[#2b374d] hover:border-[#00c2ff] bg-[#0f1217]/60'
                          }`}
                        >
                          {/* Folder Input - NO accept attribute so OS folder picker works reliably! */}
                          <input
                            type="file"
                            ref={wizardFolderInputRef}
                            webkitdirectory="true"
                            directory="true"
                            multiple
                            onChange={handleLocalFolderSelect}
                            className="hidden"
                          />
                          <span className="material-symbols-outlined text-slate-400 text-[28px] mb-1">drive_folder_upload</span>
                          
                          <div className="flex flex-col gap-2 mt-1">
                            <button
                              type="button"
                              onClick={(e) => {
                                e.stopPropagation();
                                if (wizardFolderInputRef.current) wizardFolderInputRef.current.click();
                              }}
                              className="px-3 py-1.5 bg-[#00c2ff] text-slate-950 rounded-lg text-xs font-bold hover:bg-[#38d0ff] transition-all"
                            >
                              📁 Choisir un dossier d'images
                            </button>
                          </div>
                          
                          {localImageFiles.length > 0 && (
                            <div className="mt-3 px-2.5 py-1 bg-emerald-950 text-emerald-300 rounded-lg text-[10px] font-bold font-mono truncate">
                              ✓ {selectedFolderName || 'Dossier'}: {localImageFiles.length} images
                            </div>
                          )}
                        </div>
                      </div>

                      {/* OPTION B: GÉNÉRATION IA AUTOMATIQUE */}
                      <div 
                        onClick={() => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, source: 'ai_generated' } })}
                        className={`p-5 rounded-2xl border-2 transition-all cursor-pointer space-y-4 flex flex-col justify-between ${
                          newChannel.image_style.source === 'ai_generated'
                            ? 'bg-[#1b2230] border-[#00c2ff] shadow-lg shadow-[#00c2ff]/10'
                            : 'bg-[#141923] border-[#263042] hover:border-slate-500'
                        }`}
                      >
                        <div className="space-y-2">
                          <div className="flex items-center gap-2.5">
                            <span className="material-symbols-outlined text-[#00c2ff] text-[24px]">auto_awesome</span>
                            <h4 className="font-bold text-white text-xs">Option B: Génération IA Automatique</h4>
                          </div>
                          <p className="text-[11px] text-slate-400">
                            L'IA génère automatiquement les visuels pour chaque scène (style optionnel).
                          </p>
                        </div>

                        <div>
                          <label className="block text-[10px] font-bold text-slate-400 mb-1">Style de prompt (Optionnel)</label>
                          <textarea
                            rows="2"
                            value={newChannel.image_style.style_prompt}
                            onChange={e => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, style_prompt: e.target.value } })}
                            className="w-full bg-[#0f1217] border border-[#2b374d] rounded-xl p-2.5 text-[11px] text-white focus:border-[#00c2ff] outline-none placeholder-slate-500"
                            placeholder="Optionnel: cinematic lighting, stoic sculpture style..."
                          />
                        </div>
                      </div>

                      {/* OPTION C: MODE HYBRIDE (DOSSIER LOCAL + COMPLÉMENT IA) */}
                      <div 
                        onClick={() => setNewChannel({ ...newChannel, image_style: { ...newChannel.image_style, source: 'hybrid' } })}
                        className={`p-5 rounded-2xl border-2 transition-all cursor-pointer space-y-4 flex flex-col justify-between ${
                          newChannel.image_style.source === 'hybrid'
                            ? 'bg-[#1b2230] border-[#00c2ff] shadow-lg shadow-[#00c2ff]/10'
                            : 'bg-[#141923] border-[#263042] hover:border-slate-500'
                        }`}
                      >
                        <div className="space-y-2">
                          <div className="flex items-center gap-2.5">
                            <span className="material-symbols-outlined text-[#00c2ff] text-[24px]">tune</span>
                            <h4 className="font-bold text-white text-xs">Option C: Mode Hybride (Dossier + IA)</h4>
                          </div>
                          <p className="text-[11px] text-slate-400">
                            Utilise vos images locales et laisse l'IA proposer des suggestions complémentaires pour enrichir la vidéo.
                          </p>
                        </div>

                        <div className="bg-[#0f1217]/60 p-3 rounded-xl border border-[#2b374d] text-[10px] text-slate-300 space-y-1">
                          <div className="flex items-center gap-1.5 text-emerald-400 font-bold">
                            <span className="material-symbols-outlined text-[14px]">check_circle</span>
                            Dossier local prioritaire
                          </div>
                          <div className="flex items-center gap-1.5 text-[#00c2ff] font-bold">
                            <span className="material-symbols-outlined text-[14px]">sparkles</span>
                            Complément d'images IA
                          </div>
                        </div>
                      </div>

                    </div>
                  </div>
                )}

                {/* STEP 5: APERÇU FINAL DU DESIGN VIDÉO (LIVE 16:9 LANDSCAPE PREVIEW) */}
                {wizardStep === 5 && (
                  <div className="space-y-6">
                    <div className="flex justify-between items-center">
                      <div>
                        <h3 className="text-base font-bold text-white">5. Aperçu Final du Layout & Design Vidéo</h3>
                        <p className="text-xs text-slate-400 mt-0.5">Voici le rendu final simulé — format vidéo longue durée 16:9 (YouTube).</p>
                      </div>
                      <span className="text-xs font-mono font-bold text-emerald-400 bg-emerald-950/80 px-3 py-1 rounded-lg border border-emerald-800">Format 16:9 Paysage</span>
                    </div>

                    {/* Live 16:9 Landscape Video Mockup Preview */}
                    <div className="flex justify-center">
                      <div className="w-full max-w-[640px] aspect-[16/9] bg-slate-950 rounded-2xl border-4 border-[#2b374d] relative overflow-hidden shadow-2xl flex flex-col justify-between p-5">

                        {/* Background Scene Visual — real example of a generated/library image */}
                        <div className="absolute inset-0">
                          <img
                            src={`${STORAGE_BASE}/examples/example_scene.png`}
                            alt="Exemple de scène générée"
                            className="w-full h-full object-cover opacity-80"
                          />
                          <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-black/30"></div>
                        </div>
                        <div className="absolute top-3 left-3 z-20 bg-black/60 backdrop-blur-sm px-2.5 py-1 rounded-lg text-[10px] font-mono text-slate-300 border border-white/10">
                          Exemple: {newChannel.image_style.source === 'library' ? (selectedFolderName ? `Dossier: ${selectedFolderName}` : 'Images Locales') :
                           newChannel.image_style.source === 'hybrid' ? 'Mode Hybride (Dossier + IA)' : 'Scènes générées par IA'}
                        </div>

                        {/* Top Header Bar inside Mockup */}
                        <div className="relative z-20 flex justify-between items-center text-xs text-white">
                          <div className="flex items-center gap-2 bg-slate-900/80 backdrop-blur-md px-2.5 py-1 rounded-full border border-slate-700">
                            {logoPreviewUrl ? (
                              <img src={logoPreviewUrl} alt="Logo" className="w-5 h-5 rounded-full object-cover" />
                            ) : (
                              <div className="w-5 h-5 rounded-full bg-[#00c2ff] text-slate-950 font-bold flex items-center justify-center text-[10px]">
                                {newChannel.name ? newChannel.name.slice(0, 1) : 'N'}
                              </div>
                            )}
                            <span className="font-bold text-[11px] truncate max-w-[160px]">{newChannel.name || 'Nom Chaîne'}</span>
                          </div>

                          {/* Watermark Tag Overlay */}
                          {newChannel.branding.channel_name_text && (
                            <div
                              style={{ opacity: newChannel.branding.watermark_opacity || 0.85 }}
                              className="bg-black/60 backdrop-blur-sm px-2 py-0.5 rounded text-[10px] font-mono text-slate-200 border border-white/20"
                            >
                              {newChannel.branding.channel_name_text}
                            </div>
                          )}
                        </div>

                        {/* Bottom Section: Karaoke Subtitles & Music Badge inside Mockup */}
                        <div className="relative z-20 space-y-3">

                          {/* Animated Karaoké Subtitle Rendering */}
                          <div
                            style={{
                              backgroundColor: newChannel.subtitle_style.box_color || 'transparent',
                              padding: '8px 12px',
                              borderRadius: '10px'
                            }}
                            className="flex flex-wrap justify-center items-center gap-1.5 text-center"
                          >
                            {sampleWords.map((wordObj, i) => (
                              <span
                                key={i}
                                style={{
                                  fontFamily: newChannel.subtitle_style.font,
                                  fontSize: `${(newChannel.subtitle_style.size || 44) * 0.45}px`,
                                  fontWeight: '900',
                                  color: wordObj.highlight ? (newChannel.subtitle_style.color || '#FFD700') : '#FFFFFF',
                                  textShadow: wordObj.highlight
                                    ? `0 0 12px ${newChannel.subtitle_style.color || '#FFD700'}, 0 2px 4px rgba(0,0,0,0.9)`
                                    : '0 2px 4px rgba(0,0,0,0.9)',
                                  transform: wordObj.highlight ? 'scale(1.08)' : 'scale(1)',
                                  transition: 'all 0.15s ease-in-out'
                                }}
                                className="inline-block"
                              >
                                {wordObj.text}
                              </span>
                            ))}
                          </div>

                          {/* Music Preference Indicator Badge */}
                          <div className="flex justify-center">
                            <div className="bg-black/60 backdrop-blur-md px-3 py-1 rounded-full text-[10px] text-slate-300 font-mono flex items-center gap-1.5 border border-white/10">
                              <span className="material-symbols-outlined text-[12px] text-[#00c2ff] animate-spin">music_note</span>
                              Musique: {newChannel.music_preference.track_id_or_style || 'Ambiant'} ({Math.round((newChannel.music_preference.volume || 0.15) * 100)}%)
                            </div>
                          </div>
                        </div>

                      </div>
                    </div>
                  </div>
                )}

                {/* Wizard Footer Navigation */}
                <div className="flex justify-between items-center pt-6 border-t border-[#263042]">
                  {wizardStep > 1 ? (
                    <button 
                      onClick={() => setWizardStep(wizardStep - 1)}
                      className="px-6 py-2.5 rounded-xl bg-[#1b2230] text-white font-bold text-xs hover:bg-[#252f42] transition-colors"
                    >
                      Retour
                    </button>
                  ) : <div></div>}

                  {wizardStep < 5 ? (
                    <button 
                      onClick={() => setWizardStep(wizardStep + 1)}
                      className="px-6 py-2.5 rounded-xl bg-[#00c2ff] text-slate-950 font-bold text-xs hover:bg-[#38d0ff] transition-all flex items-center gap-2 shadow-md"
                    >
                      Suivant <span className="material-symbols-outlined text-[18px]">arrow_forward</span>
                    </button>
                  ) : (
                    <button
                      onClick={handleSaveChannel}
                      disabled={loading}
                      className="px-8 py-3 rounded-xl bg-emerald-500 text-slate-950 font-bold text-xs hover:bg-emerald-400 transition-all flex items-center gap-2 shadow-lg shadow-emerald-500/20"
                    >
                      <span className="material-symbols-outlined text-[18px]">check</span>
                      {loading ? "Enregistrement..." : (wizardMode === 'edit' ? "Enregistrer les modifications" : "Créer le Pipeline")}
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </main>

      {/* NOUVELLE VIDÉO MAIN ACTION MODAL */}
      {showSubmitModal && (
        <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-md z-50 flex items-center justify-center p-6">
          <div className="bg-[#161b22] border border-[#263042] rounded-3xl p-8 max-w-[620px] w-full shadow-2xl space-y-6">
            <div className="flex justify-between items-center border-b border-[#263042] pb-4">
              <div>
                <h3 className="text-lg font-extrabold text-white flex items-center gap-2">
                  <span className="material-symbols-outlined text-[#00c2ff]">movie_filter</span>
                  Générer une Nouvelle Vidéo
                </h3>
                <p className="text-xs text-slate-400 mt-1">Sélectionnez le canal et le contenu pour lancer le montage.</p>
              </div>
              <button onClick={() => setShowSubmitModal(false)} className="text-slate-400 hover:text-white p-1">
                <span className="material-symbols-outlined">close</span>
              </button>
            </div>

            {/* Target Channel Selector */}
            <div>
              <label className="block text-xs font-bold text-slate-300 mb-2">Chaîne Cible</label>
              <select
                value={activeChannel ? activeChannel.id : ''}
                onChange={e => {
                  const selected = channels.find(c => c.id === e.target.value);
                  if (selected) setActiveChannel(selected);
                }}
                className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-3 text-sm text-white focus:border-[#00c2ff] outline-none"
              >
                {channels.map(c => (
                  <option key={c.id} value={c.id}>{c.name} ({c.niche})</option>
                ))}
              </select>
            </div>

            {/* Input Mode Selector */}
            <div className="grid grid-cols-2 gap-3 bg-[#11151c] p-1.5 rounded-xl border border-[#202938]">
              <button
                type="button"
                onClick={() => setSubmitMode('text')}
                className={`py-2.5 rounded-lg text-xs font-bold transition-all ${
                  submitMode === 'text' ? 'bg-[#00c2ff] text-slate-950 shadow-md' : 'text-slate-400'
                }`}
              >
                Texte Script (Izivoice)
              </button>
              <button
                type="button"
                onClick={() => setSubmitMode('audio_upload')}
                className={`py-2.5 rounded-lg text-xs font-bold transition-all ${
                  submitMode === 'audio_upload' ? 'bg-[#00c2ff] text-slate-950 shadow-md' : 'text-slate-400'
                }`}
              >
                Fichiers Audio Importés
              </button>
            </div>

            {/* Voice Model Selection */}
            {submitMode === 'text' && (
              <div>
                <label className="block text-xs font-bold text-slate-300 mb-2">Modèle de Voix Off IA</label>
                <select
                  value={selectedVoice}
                  onChange={e => setSelectedVoice(e.target.value)}
                  className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl px-4 py-2.5 text-xs text-white focus:border-[#00c2ff] outline-none"
                >
                  {VOICE_MODELS.map(v => (
                    <option key={v.id} value={v.id}>{v.name} — {v.desc}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Content Input Area */}
            {submitMode === 'text' ? (
              <div>
                <label className="block text-xs font-bold text-slate-300 mb-2">Texte du Script</label>
                <textarea
                  rows="5"
                  value={singleScriptText}
                  onChange={e => setSingleScriptText(e.target.value)}
                  className="w-full bg-[#1b2230] border border-[#2b374d] rounded-2xl p-4 text-xs text-white focus:border-[#00c2ff] outline-none placeholder-slate-500"
                  placeholder="Collez ici le texte de votre vidéo. L'IA générera la voix off et calera les sous-titres karaoké..."
                />
              </div>
            ) : (
              <div
                onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
                onDragLeave={() => setIsDragging(false)}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current && fileInputRef.current.click()}
                className={`border-2 border-dashed rounded-2xl p-8 text-center cursor-pointer transition-all ${
                  isDragging ? 'border-[#00c2ff] bg-[#00c2ff]/10' : 'border-[#2b374d] hover:border-slate-400 bg-[#11151c]'
                }`}
              >
                <input
                  type="file"
                  ref={fileInputRef}
                  multiple
                  accept="audio/*"
                  onChange={e => setAudioFilesList(prev => [...prev, ...Array.from(e.target.files)])}
                  className="hidden"
                />
                <span className="material-symbols-outlined text-slate-400 text-[42px] mb-2">cloud_upload</span>
                <div className="text-xs font-bold text-white">Glisser-déposer vos fichiers audio (.mp3, .wav)</div>
                <div className="text-[11px] text-slate-400 mt-1">ou cliquez pour choisir des fichiers</div>
                {audioFilesList.length > 0 && (
                  <div className="mt-4 space-y-1">
                    {audioFilesList.map((f, i) => (
                      <div key={i} className="text-xs font-mono text-emerald-400 bg-emerald-950/60 p-2 rounded-lg border border-emerald-800">
                        {f.name}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Launch Action */}
            <button
              onClick={handleSubjectSubmit}
              disabled={loading}
              className="w-full py-3.5 bg-gradient-to-r from-[#00c2ff] to-[#0088ff] text-slate-950 font-bold text-sm rounded-xl hover:opacity-90 transition-all flex items-center justify-center gap-2 shadow-lg shadow-[#00c2ff]/25"
            >
              <span className="material-symbols-outlined text-[20px]">rocket_launch</span>
              {loading ? "Chargement & Lancement..." : "Lancer le Montage"}
            </button>
          </div>
        </div>
      )}

      {/* VIDEO PLAYER MODAL */}
      {selectedVideo && (
        <div className="fixed inset-0 bg-slate-950/90 backdrop-blur-md z-50 flex items-center justify-center p-6">
          <div className="bg-[#161b22] border border-[#263042] rounded-3xl p-6 max-w-[480px] w-full shadow-2xl space-y-4">
            <div className="flex justify-between items-center">
              <h3 className="text-sm font-bold text-white">Aperçu Vidéo Rendu</h3>
              <button onClick={() => setSelectedVideo(null)} className="text-slate-400 hover:text-white">
                <span className="material-symbols-outlined">close</span>
              </button>
            </div>

            <div className="aspect-[16/9] bg-black rounded-2xl overflow-hidden border border-[#263042]">
              <video
                src={`${STORAGE_BASE}/${selectedVideo.output_path?.replace('storage/', '')}`}
                controls
                autoPlay
                className="w-full h-full object-contain"
              />
            </div>

            <div className="flex justify-between items-center pt-2">
              <a
                href={`${STORAGE_BASE}/${selectedVideo.output_path?.replace('storage/', '')}`}
                download
                target="_blank"
                rel="noreferrer"
                className="w-full py-3 bg-[#00c2ff] text-slate-950 font-bold text-xs rounded-xl text-center hover:bg-[#38d0ff] transition-all flex items-center justify-center gap-2"
              >
                <span className="material-symbols-outlined text-[18px]">download</span> Télécharger MP4
              </a>
            </div>
          </div>
        </div>
      )}

      {/* USER AUTH MODAL */}
      {showAuthModal && (
        <div className="fixed inset-0 bg-slate-950/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
          <div className="bg-[#161b22] border border-[#263042] rounded-3xl p-8 max-w-[440px] w-full shadow-2xl space-y-6">
            <div className="flex justify-between items-center">
              <h3 className="text-lg font-extrabold text-white">
                {authTab === 'login' && 'Connexion'}
                {authTab === 'register' && 'Inscription'}
              </h3>
              <button onClick={() => setShowAuthModal(false)} className="text-slate-400 hover:text-white">
                <span className="material-symbols-outlined">close</span>
              </button>
            </div>

            <form onSubmit={handleAuthSubmit} className="space-y-4">
              <div>
                <label className="block text-xs font-bold text-slate-300 mb-1">Adresse Email</label>
                <input 
                  type="email"
                  required
                  value={authForm.email}
                  onChange={e => setAuthForm({ ...authForm, email: e.target.value })}
                  className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl p-3 text-xs text-white focus:border-[#00c2ff] outline-none"
                  placeholder="nom@exemple.com"
                />
              </div>
              <div>
                <label className="block text-xs font-bold text-slate-300 mb-1">Mot de passe</label>
                <input 
                  type="password"
                  required
                  value={authForm.password}
                  onChange={e => setAuthForm({ ...authForm, password: e.target.value })}
                  className="w-full bg-[#1b2230] border border-[#2b374d] rounded-xl p-3 text-xs text-white focus:border-[#00c2ff] outline-none"
                  placeholder="••••••••"
                />
              </div>
              <button 
                type="submit"
                className="w-full py-3 bg-[#00c2ff] text-slate-950 font-bold text-xs rounded-xl hover:bg-[#38d0ff] transition-all mt-4"
              >
                {authTab === 'register' ? "Créer mon compte" : "Se connecter"}
              </button>
            </form>
          </div>
        </div>
      )}

      {/* USER PROFILE & SETTINGS MODAL (INTEGRATED PARAMÈTRES & PROFIL) */}
      {showProfileModal && currentUser && (
        <div className="fixed inset-0 bg-slate-950/85 backdrop-blur-md z-50 flex items-center justify-center p-6">
          <div className="bg-[#161b22] border border-[#263042] rounded-3xl p-8 max-w-[640px] w-full max-h-[90vh] overflow-y-auto shadow-2xl space-y-6">
            <div className="flex justify-between items-center border-b border-[#263042] pb-4">
              <div className="flex items-center gap-3">
                <div className="w-11 h-11 rounded-2xl bg-[#00c2ff] text-slate-950 flex items-center justify-center font-extrabold text-lg shadow-md">
                  {currentUser.name.slice(0, 1).toUpperCase()}
                </div>
                <div>
                  <h3 className="text-lg font-extrabold text-white">{currentUser.name}</h3>
                  <p className="text-xs text-slate-400">{currentUser.email}</p>
                </div>
              </div>
              <button onClick={() => setShowProfileModal(false)} className="text-slate-400 hover:text-white p-1">
                <span className="material-symbols-outlined">close</span>
              </button>
            </div>

            <div className="space-y-4">
              <h4 className="text-sm font-bold text-white font-mono">Informations du Compte</h4>
              <div className="text-xs text-slate-400 space-y-1">
                <p>Nom: <strong className="text-white">{currentUser.name}</strong></p>
                <p>Email: <strong className="text-white">{currentUser.email}</strong></p>
              </div>
            </div>

            <div className="pt-4 border-t border-[#263042] flex justify-between items-center">
              <button
                onClick={handleLogout}
                className="px-4 py-2 bg-rose-950/70 text-rose-300 border border-rose-800 rounded-xl font-bold text-xs hover:bg-rose-900 transition-all flex items-center gap-2"
              >
                <span className="material-symbols-outlined text-[16px]">logout</span> Deconnexion
              </button>
            </div>
          </div>
        </div>
      )}

      {/* CHANNEL PICKER MODAL (when Nouvelle Vidéo clicked without active channel preset) */}
      {showChannelPickerModal && (
        <div className="fixed inset-0 bg-slate-950/80 backdrop-blur-md z-50 flex items-center justify-center p-6">
          <div className="bg-[#161b22] border border-[#263042] rounded-3xl p-8 max-w-[480px] w-full shadow-2xl space-y-6">
            <div className="flex justify-between items-center border-b border-[#263042] pb-4">
              <h3 className="text-base font-extrabold text-white">Choisir une chaîne pour la vidéo</h3>
              <button onClick={() => setShowChannelPickerModal(false)} className="text-slate-400 hover:text-white">
                <span className="material-symbols-outlined">close</span>
              </button>
            </div>

            <div className="space-y-3 max-h-[300px] overflow-y-auto">
              {channels.map(chan => (
                <div
                  key={chan.id}
                  onClick={() => {
                    setActiveChannel(chan);
                    setShowChannelPickerModal(false);
                    setShowSubmitModal(true);
                  }}
                  className="p-4 bg-[#1b2230] hover:bg-[#252f42] border border-[#2b374d] rounded-2xl cursor-pointer flex items-center gap-4 transition-all"
                >
                  <div className="w-10 h-10 rounded-xl bg-[#00c2ff] text-slate-950 flex items-center justify-center font-bold text-sm">
                    {chan.name.slice(0, 2).toUpperCase()}
                  </div>
                  <div>
                    <h4 className="text-sm font-bold text-white">{chan.name}</h4>
                    <p className="text-xs text-slate-400">{chan.niche}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* TOAST NOTIFICATION */}
      {toast && (
        <div className="fixed bottom-6 right-6 z-[100] animate-in fade-in slide-in-from-bottom-4 duration-300">
          <div className={`flex items-center gap-3 px-5 py-3.5 rounded-xl shadow-2xl border max-w-md ${
            toast.type === 'error'
              ? 'bg-rose-950 border-rose-800 text-rose-200'
              : 'bg-emerald-950 border-emerald-800 text-emerald-200'
          }`}>
            <span className="material-symbols-outlined text-[20px]">
              {toast.type === 'error' ? 'error' : 'check_circle'}
            </span>
            <span className="text-sm font-medium">{toast.message}</span>
            <button onClick={() => setToast(null)} className="ml-2 opacity-70 hover:opacity-100">
              <span className="material-symbols-outlined text-[16px]">close</span>
            </button>
          </div>
        </div>
      )}

    </div>
  );
}
